"""Executor node: execute planned MCP tool calls.

截断语义说明（来自 MCP 服务器）：
- meta.truncated=true：单条记录内容被裁短（长堆栈/长响应截取头尾），
  不代表还有更多记录可查，无法通过继续查询获得更多信息。
- hit_log_limit=true：查询结果触及 limit 上限，可能还有更多记录，
  可以通过扩大时间窗、拆分时间窗或工具 schema 明确支持的分页参数获取更多数据。
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter

from agent.persistence import add_tool_call, args_hash
from agent.state import AgentState, Evidence
from agent.tools.mcp_client import ClientSession, call_tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Evidence summary extraction — 从 MCP JSON 返回中提取诊断关键字段
# MCP 已对每条日志 content 独立裁剪，此处进一步精简为结构化摘要
# ---------------------------------------------------------------------------

_ERROR_KEYWORDS = ("ERROR", "Exception", "error", "WARN", "FATAL", "failed", "failure")

TRACE_SUMMARY_LIMIT = 3000
OVERVIEW_SUMMARY_LIMIT = 2000
REQUEST_RESPONSE_SUMMARY_LIMIT = 2000
ERRORS_SUMMARY_LIMIT = 4000
FALLBACK_LIMIT = 1500


def _has_error_signal(text: str) -> bool:
    """Check if text contains error-related keywords."""
    return any(kw in text for kw in _ERROR_KEYWORDS)


def _extract_evidence_trace_id(args: dict) -> str | None:
    trace_id = args.get("trace_id")
    if isinstance(trace_id, str) and trace_id:
        return trace_id
    for item in args.get("filters", []) or []:
        if item.get("field") == "trace_id" and isinstance(item.get("value"), str):
            return item["value"]
    return None


def _summarize_trace(data: dict) -> str:
    """Extract summary from sls_get_trace result."""
    parts: list[str] = []

    # summary block
    summary = data.get("summary", {})
    if summary:
        parts.append(
            f"[summary] model={summary.get('model')}, provider={summary.get('provider')}, "
            f"status={summary.get('status')}, latency={summary.get('latency_ms')}ms"
        )

    # diagnosis_hints — structured signals from MCP, prefer over raw event scanning
    hints = data.get("diagnosis_hints", {})
    if hints:
        hint_parts: list[str] = []
        if hints.get("exception_classes"):
            hint_parts.append(f"异常类: {', '.join(hints['exception_classes'])}")
        if hints.get("http_status_codes"):
            hint_parts.append(f"HTTP状态码: {', '.join(hints['http_status_codes'])}")
        if hints.get("error_source"):
            hint_parts.append(f"错误来源: {hints['error_source']}")
        if hints.get("tokens_zero"):
            hint_parts.append("tokens=0（请求未到达推理）")
        if hints.get("has_upstream_error"):
            hint_parts.append("上游返回错误")
        if hints.get("upstream_error_message"):
            hint_parts.append(f"上游错误信息: {hints['upstream_error_message']}")
        if hints.get("upstream_error_type"):
            hint_parts.append(f"上游错误类型: {hints['upstream_error_type']}")
        if not hints.get("has_response"):
            hint_parts.append("无响应内容")
        if hints.get("has_error_stacktrace"):
            hint_parts.append("包含错误堆栈")
        if hints.get("error_preview"):
            hint_parts.append(f"错误预览: {hints['error_preview']}")
        if hint_parts:
            parts.append("[diagnosis_hints] " + "; ".join(hint_parts))

    # events — 按诊断价值分配截取长度
    for event in data.get("events", []):
        logstore = event.get("logstore", "")
        content = str(event.get("content", ""))
        log_type = event.get("log_type", "")
        time_str = event.get("time", "")
        header = f"[{logstore}/{log_type}] @{time_str}"

        if _has_error_signal(content):
            # 错误日志：保留 1500 字符（覆盖异常类名+错误码+前几行堆栈）
            parts.append(f"{header}\n{content[:1500]}")
        elif logstore == "gateway":
            parts.append(f"{header}\n{content[:500]}")
        elif logstore == "request_response":
            parts.append(f"{header}\n{content[:300]}")
        else:
            parts.append(f"{header}\n{content[:500]}")

    # assembled response
    assembled = data.get("assembled", {})
    resp = assembled.get("response", "")
    if resp:
        parts.append(
            f"[assembled_response] format={assembled.get('response_format')}\n{resp[:500]}"
        )

    text = "\n".join(parts)
    return text[:TRACE_SUMMARY_LIMIT]


def _summarize_overview(data: dict) -> str:
    """Extract summary from sls_query_gateway_usage_overview result."""
    records = data.get("records", [])
    if len(records) > 20:
        return _summarize_overview_aggregated(records)
    # Detailed mode for small batches
    parts: list[str] = []
    for r in records:
        line = (
            f"trace={r.get('trace_id')} status={r.get('status')} "
            f"model={r.get('model')} provider={r.get('provider')} "
            f"latency={r.get('latency_ms')}ms "
            f"tokens=({r.get('input_tokens')}/{r.get('output_tokens')}) "
            f"key={r.get('api_key_masked')} time={r.get('request_time')}"
        )
        err = r.get("error_detail")
        if err:
            line += f"\nerror: {str(err)[:500]}"
        parts.append(line)
    text = "\n".join(parts)
    return text[:OVERVIEW_SUMMARY_LIMIT] if text else "(空结果)"


def _summarize_overview_aggregated(records: list) -> str:
    """Aggregate summary for large overview batches (>20 records).

    Outputs distribution stats + pattern detection instead of per-record details.
    """
    models = Counter(r.get("model") for r in records)
    statuses = Counter(r.get("status") for r in records)
    providers = Counter(r.get("provider") for r in records)
    error_records = [r for r in records if r.get("error_detail")]
    error_types = Counter(str(r.get("error_detail", ""))[:100] for r in error_records)

    parts = [
        f"共 {len(records)} 条记录",
        f"模型分布: {dict(models.most_common(5))}",
        f"状态分布: {dict(statuses)}",
        f"供应商分布: {dict(providers.most_common(5))}",
        f"错误记录: {len(error_records)} 条",
    ]

    # Error type distribution (deduplicated)
    if error_types:
        parts.append(f"错误类型分布: {dict(error_types.most_common(5))}")

    # Pattern detection: errors concentrated on one provider
    if error_records:
        error_providers = Counter(r.get("provider") for r in error_records)
        top_provider, top_count = error_providers.most_common(1)[0]
        if top_count > len(error_records) * 0.7:
            parts.append(
                f"错误集中在 provider={top_provider}"
                f"（{top_count}/{len(error_records)} 条）"
            )

    # Time range of errors
    if error_records:
        times = [r.get("request_time", "") for r in error_records if r.get("request_time")]
        if times:
            parts.append(f"最早错误: {min(times)}, 最晚错误: {max(times)}")

    return "\n".join(parts)


def _summarize_aggregate_gateway_usage(data: dict) -> str:
    """Extract complete compact summary from sls_aggregate_gateway_usage result."""
    records = data.get("records", [])
    if not records:
        return "(空结果)"

    total_request = sum(int(r.get("request_count") or 0) for r in records)
    total_success = sum(int(r.get("success_count") or 0) for r in records)
    total_failed = sum(int(r.get("failed_count") or 0) for r in records)
    parts = [
        f"聚合行数: {len(records)}",
        f"合计: request_count={total_request}, success_count={total_success}, failed_count={total_failed}",
    ]

    for r in records:
        dimensions = []
        for field in ("time_bucket", "provider", "model", "status", "api_key_masked", "user_agent", "client_ip", "host", "base_path", "fc_request_id"):
            value = r.get(field)
            if value not in (None, ""):
                dimensions.append(f"{field}={value}")
        metrics = [
            f"request_count={r.get('request_count')}",
            f"success_count={r.get('success_count')}",
            f"failed_count={r.get('failed_count')}",
        ]
        if r.get("avg_latency_ms") is not None:
            metrics.append(f"avg_latency_ms={r.get('avg_latency_ms')}")
        if r.get("max_latency_ms") is not None:
            metrics.append(f"max_latency_ms={r.get('max_latency_ms')}")
        parts.append("; ".join(dimensions + metrics))

    return "\n".join(parts)


def _summarize_request_response(data: dict) -> str:
    """Extract summary from sls_get_request_response result."""
    parts: list[str] = []

    assembled = data.get("assembled", {})
    resp = assembled.get("response", "")
    if resp:
        parts.append(
            f"[assembled_response] format={assembled.get('response_format')} "
            f"chunks={assembled.get('response_chunk_count')}\n{resp[:800]}"
        )

    for event in data.get("events", []):
        content = str(event.get("content", ""))
        log_type = event.get("log_type", "")
        if _has_error_signal(content):
            parts.append(f"[{log_type}] {content[:800]}")
        else:
            parts.append(f"[{log_type}] {content[:300]}")

    text = "\n".join(parts)
    return text[:REQUEST_RESPONSE_SUMMARY_LIMIT]


def _summarize_errors(data: dict) -> str:
    """Extract summary from sls_search_errors result."""
    parts: list[str] = []
    for e in data.get("errors", [])[:5]:
        msg = str(e.get("message", ""))[:800]
        parts.append(f"trace={e.get('trace_id')} time={e.get('__time__')}\n{msg}")
    text = "\n---\n".join(parts)
    return text[:ERRORS_SUMMARY_LIMIT] if text else "(无错误记录)"


def _summarize_db(data: dict) -> str:
    """Extract summary from db_get_user_api_key_prefixes result."""
    return json.dumps(data, ensure_ascii=False)[:FALLBACK_LIMIT]


def _summarize_code_explore(data: dict) -> str:
    """Extract summary from code_explore result."""
    summary = data.get("summary", "")
    findings = data.get("key_findings", [])
    refs = data.get("code_refs", [])
    files = data.get("files_read", [])
    steps = data.get("steps", 0)
    error = data.get("error")
    partial = data.get("partial")

    parts = []
    if error:
        parts.append(f"[代码探索失败] error={error}; steps={steps}; partial={bool(partial)}")
    elif partial:
        parts.append(f"[代码探索未完成] steps={steps}; partial=true")
    if summary:
        label = "失败摘要" if error else "代码探索结论"
        parts.append(f"[{label}] {summary}")
    if findings:
        parts.append("关键发现: " + "; ".join(str(f) for f in findings[:5]))
    if refs:
        parts.append("代码引用: " + "; ".join(str(r) for r in refs[:5]))
    if files:
        parts.append(f"阅读文件: {', '.join(str(f) for f in files[:10])}")
    if steps and not error:
        parts.append(f"探索步数: {steps}")

    return "\n".join(parts) if parts else str(data)[:FALLBACK_LIMIT]


_SUMMARIZERS = {
    "sls_get_trace": _summarize_trace,
    "sls_query_gateway_usage_overview": _summarize_overview,
    "sls_aggregate_gateway_usage": _summarize_aggregate_gateway_usage,
    "sls_get_request_response": _summarize_request_response,
    "sls_search_errors": _summarize_errors,
    "db_get_user_api_key_prefixes": _summarize_db,
    "code_explore": _summarize_code_explore,
}


def _extract_summary(tool_name: str, result: str) -> str:
    """Parse MCP JSON result and extract diagnosis-relevant fields.

    每个工具有独立的提取策略，聚焦错误信息和关键元数据。
    JSON 解析失败或未知工具时 fallback 到前 1500 字符。
    """
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return result[:FALLBACK_LIMIT]

    # code_* tools return JSON directly without MCP {ok, data} envelope
    if tool_name.startswith("code_"):
        fn = _SUMMARIZERS.get(tool_name)
        if fn:
            return fn(data)
        return result[:FALLBACK_LIMIT]

    if not data.get("ok"):
        error = data.get("error", {})
        return (
            f"ERROR: {error.get('code')} - {error.get('message')} "
            f"({error.get('safe_detail', '')})"
        )

    fn = _SUMMARIZERS.get(tool_name)
    if fn:
        return fn(data.get("data", {}))
    return result[:FALLBACK_LIMIT]


# ---------------------------------------------------------------------------
# Truncation parsing
# ---------------------------------------------------------------------------

def _parse_truncation(result: str) -> tuple[bool, bool]:
    """Parse MCP result to distinguish content_truncated vs hit_log_limit.

    Returns:
        (content_truncated, hit_log_limit)
        - content_truncated: 单条记录内容被裁短，无法通过继续查询恢复
        - hit_log_limit: 查询结果触及上限，可能还有更多记录
    """
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return False, False

    # 顶层 meta
    meta = data.get("meta", {})
    content_truncated = bool(meta.get("truncated", False))
    hit_limit = bool(meta.get("hit_log_limit", False))

    # 某些工具在不同层级返回 hit_log_limit
    if not hit_limit and isinstance(data, dict):
        hit_limit = bool(data.get("hit_log_limit", False))

    return content_truncated, hit_limit


def _filter_args_by_schema(tool_name: str, tool_args: dict, mcp_tools: list[dict]) -> dict:
    """Filter tool args to only include parameters defined in the MCP tool schema.

    Prevents LLM-hallucinated parameters from reaching MCP tools.
    """
    schema = None
    for t in mcp_tools:
        if t["name"] == tool_name:
            schema = t.get("input_schema", {})
            break
    if not schema:
        return tool_args
    allowed = set(schema.get("properties", {}).keys())
    if not allowed:
        return tool_args
    filtered = {k: v for k, v in tool_args.items() if k in allowed}
    removed = set(tool_args.keys()) - allowed
    if removed:
        logger.warning("Filtered hallucinated params from %s: %s", tool_name, removed)
    return filtered


async def run_executor(
    state: AgentState,
    session: ClientSession,
    mcp_tools: list[dict] | None = None,
    on_event=None,
) -> AgentState:
    """Execute tool calls from state.plan, dedup by hash, accumulate evidence."""

    def emit(event_type: str, data=None):
        if on_event:
            on_event(event_type, data)

    for planned in state.plan:
        if not state.can_call_more_tools:
            emit("tool_limit", {
                "max_total": state.MAX_TOTAL_TOOL_CALLS,
                "max_query": state.MAX_QUERY_TOOL_CALLS,
                "total_tool_calls": state.total_tool_calls,
                "query_tool_calls": state.query_tool_calls,
            })
            break

        tool_name = planned["tool"]
        tool_args = planned.get("args", {})

        # Filter args against MCP schema to prevent hallucinated parameters
        if mcp_tools:
            tool_args = _filter_args_by_schema(tool_name, tool_args, mcp_tools)

        # Dedup check
        h = args_hash(tool_name, tool_args)
        if h in state.executed_hashes:
            emit("tool_dedup", {"name": tool_name, "args": tool_args})
            continue

        state.executed_hashes.add(h)
        emit("tool_call", {"name": tool_name, "input": tool_args})

        t0 = time.time()
        error_msg = None
        try:
            if tool_name.startswith("code_"):
                from agent.tools.code_reader import execute_code_tool
                result = execute_code_tool(tool_name, tool_args)
            else:
                result = await call_tool(session, tool_name, tool_args)
        except Exception as e:
            result = f"ERROR: {e}"
            error_msg = str(e)
        latency_ms = int((time.time() - t0) * 1000)
        result_size = len(result)

        # 区分两种截断语义
        content_truncated, hit_log_limit = _parse_truncation(result)

        emit("tool_result", {
            "name": tool_name,
            "result_len": result_size,
            "latency_ms": latency_ms,
            "content_truncated": content_truncated,
            "hit_log_limit": hit_log_limit,
        })

        # Persist tool call
        add_tool_call(
            thread_id=state.thread_id,
            tool=tool_name,
            args=tool_args,
            result_size=result_size,
            truncated=content_truncated,
            latency_ms=latency_ms,
            error=error_msg,
        )

        # Accumulate evidence
        summary = _extract_summary(tool_name, result)
        try:
            from agent.debug import info_all
            info_all.record_tool(tool_name, tool_args, result, summary, {
                "result_size": result_size,
                "latency_ms": latency_ms,
                "content_truncated": content_truncated,
                "hit_log_limit": hit_log_limit,
                "error": error_msg,
            })
        except Exception:
            pass
        evidence = Evidence(
            tool=tool_name,
            args=tool_args,
            result=result,
            result_size=result_size,
            truncated=content_truncated,
            hit_log_limit=hit_log_limit,
            latency_ms=latency_ms,
            summary=summary,
            trace_id=_extract_evidence_trace_id(tool_args),
        )
        state.evidence.append(evidence)
        state.query_tool_calls += 1

        # 只有 hit_log_limit 才需要累加器（还有更多数据可查）
        if hit_log_limit:
            acc_key = tool_name
            if acc_key not in state.accumulator:
                state.accumulator[acc_key] = []
            state.accumulator[acc_key].append(result)

    # Clear plan after execution
    state.plan = []
    return state

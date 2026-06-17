"""Full-chain debug artifact writer for agent runs."""
from __future__ import annotations

import json
import re
import shutil
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from agent.config import AGENT_DEBUG_INFO_ALL, AGENT_DEBUG_INFO_ALL_DIR

_current: ContextVar["DebugRun | None"] = ContextVar("debug_run", default=None)

_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_-]{16,})"),
    re.compile(r"(th-[A-Za-z0-9_-]{16,})"),
    re.compile(r"(Bearer\s+)([A-Za-z0-9._~+/=-]{16,})", re.I),
    re.compile(r"(Authorization\s*[:=]\s*)([^\s,}]+)", re.I),
    re.compile(r"(api[_-]?key\s*[:=]\s*)([^\s,}]+)", re.I),
    re.compile(r"(cookie\s*[:=]\s*)([^\n}]+)", re.I),
]


def _now() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S.%f%z")


def _mask_secret(value: str) -> str:
    def keep_prefix(match: re.Match) -> str:
        token = match.group(1)
        return token[:8] + "..." + token[-4:] if len(token) > 16 else token

    value = _SECRET_PATTERNS[0].sub(keep_prefix, value)
    value = _SECRET_PATTERNS[1].sub(keep_prefix, value)
    value = _SECRET_PATTERNS[2].sub(lambda m: m.group(1) + m.group(2)[:8] + "..." + m.group(2)[-4:], value)
    value = _SECRET_PATTERNS[3].sub(lambda m: m.group(1) + "***", value)
    value = _SECRET_PATTERNS[4].sub(lambda m: m.group(1) + "***", value)
    value = _SECRET_PATTERNS[5].sub(lambda m: m.group(1) + "***", value)
    return value


def sanitize(obj: Any) -> Any:
    """JSON-safe conversion with minimal secret masking."""
    if is_dataclass(obj):
        obj = asdict(obj)
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, str):
        return _mask_secret(obj)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _mask_secret(str(obj))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize(data), ensure_ascii=False, indent=2), encoding="utf-8")


class DebugRun:
    def __init__(
        self,
        session_id: str,
        question_index: int,
        thread_id: str,
        user_query: str,
        base_dir: str | Path | None = None,
        run_name: str | None = None,
    ):
        self.session_id = session_id
        self.question_index = question_index
        self.thread_id = thread_id
        self.user_query = user_query
        root = Path(base_dir) if base_dir is not None else Path(AGENT_DEBUG_INFO_ALL_DIR)
        turn_dir = run_name or f"{question_index:03d}-{thread_id}"
        self.session_dir = root / session_id
        self.base_dir = self.session_dir / turn_dir
        self.llm_count = 0
        self.tool_count = 0

    def start(self, extra: dict[str, Any] | None = None) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.write_config({
            "session_id": self.session_id,
            "question_index": self.question_index,
            "thread_id": self.thread_id,
            "user_query": self.user_query,
            "started_at": _now(),
            **(extra or {}),
        })

    def write_config(self, data: dict[str, Any]) -> None:
        existing: dict[str, Any] = {}
        path = self.base_dir / "config.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        existing.update(data)
        _write_json(path, existing)

    def event(self, event_type: str, data: Any = None) -> None:
        path = self.base_dir / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = sanitize({"time": _now(), "event": event_type, "data": data})
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write(self, relative_path: str, data: Any) -> None:
        _write_json(self.base_dir / relative_path, data)

    def discard(self) -> None:
        if self.base_dir.exists():
            shutil.rmtree(self.base_dir)

    def llm(self, node: str, payload: dict[str, Any], response: dict[str, Any] | None = None) -> None:
        self.llm_count += 1
        record = dict(payload)
        record["response"] = response
        self.write(f"llm/{self.llm_count:02d}-{node}.json", record)

    def tool(self, tool_name: str, args: dict[str, Any], raw_result: str, summary: str, meta: dict[str, Any]) -> None:
        self.tool_count += 1
        self.write(f"tools/{self.tool_count:02d}-{tool_name}.json", {
            "tool": tool_name,
            "args": args,
            "meta": meta,
            "summary_for_llm": summary,
            "raw_result": raw_result,
        })

    def session_cli(self, event_type: str, data: Any = None) -> None:
        line = _format_cli_event(event_type, data)
        if not line:
            return
        path = self.session_dir / "session-cli.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def append_session_file(self, filename: str, content: str) -> None:
        path = self.session_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content.rstrip() + "\n")


def _short(value: Any, limit: int = 200) -> str:
    text = _mask_secret(str(sanitize(value)))
    return text if len(text) <= limit else text[:limit] + "..."


def _format_cli_event(event_type: str, data: Any = None) -> str | None:
    data = data or {}
    if event_type == "turn_start":
        return f"\n## Turn {data.get('question_index', '?')}\n\n> {data.get('query', '')}\n"
    if event_type == "node_start":
        return f"- [{data.get('node', '')}] 开始"
    if event_type == "node_end":
        info = ", ".join(f"{k}={v}" for k, v in data.items() if k != "node")
        return f"- [{data.get('node', '')}] 完成" + (f" {info}" if info else "")
    if event_type == "loop_start":
        return f"- 排查迭代 {data.get('iteration')}"
    if event_type == "loop_continue":
        return "- 证据不足，继续排查"
    if event_type == "plan_detail":
        lines = ["- planner 计划明细"]
        for i, item in enumerate(data.get("plan", []), 1):
            lines.append(f"  - {i}. {item.get('tool', '')}: {_short(item.get('args', {}), 500)}")
        return "\n".join(lines)
    if event_type == "tool_call":
        return f"- 调用工具 {data.get('name')}: {_short(data.get('input', {}))}"
    if event_type == "tool_result":
        flags = []
        if data.get("content_truncated"):
            flags.append("裁短")
        if data.get("hit_log_limit"):
            flags.append("触及上限")
        flag_text = f" ({', '.join(flags)})" if flags else ""
        return f"- 工具返回 {data.get('result_len')} 字节, {data.get('latency_ms', 0)}ms{flag_text}"
    if event_type == "tool_dedup":
        return f"- 跳过重复工具 {data.get('name')}"
    if event_type == "tool_limit":
        if "max_query" in data:
            return (
                f"- 已达工具调用上限: 本问题 {data.get('query_tool_calls')}/{data.get('max_query')}, "
                f"state {data.get('total_tool_calls')}/{data.get('max_total')}"
            )
        return f"- 已达工具调用上限 {data.get('max')}"
    if event_type == "no_plan":
        return "- planner 无计划，进入报告生成"
    if event_type == "skill_reroute":
        return f"- 证据路由命中 Skill: {data.get('skills')}"
    if event_type == "evidence_compress":
        return (
            f"- Evidence 已压缩: 新增 {data.get('compressed')} 条摘要，"
            f"保留最近 {data.get('keep_recent_turns')} 轮原始证据"
        )
    if event_type == "history_compress":
        return f"- History 已压缩: {data.get('before')} -> {data.get('after')} 条"
    if event_type == "rag_history_index":
        if data.get("status") == "indexed":
            return f"- RAG 历史库已保存当前案例: {data.get('chunk_id')}"
        if data.get("reason") == "near_duplicate":
            return f"- RAG 历史库跳过保存: 近似重复 score={data.get('score')} similar_to={data.get('similar_to')}"
        return f"- RAG 历史库跳过保存: {data.get('reason')}"
    if event_type == "rag_hit":
        label = {"history": "历史案例", "skill": "Skill"}.get(data.get("type"), data.get("type"))
        return f"- RAG 命中 {label}: {data.get('count')} 条"
    if event_type == "skill_inherit":
        return f"- 继承前轮 Skill: {data.get('skills')} ({data.get('reason')})"
    if event_type == "skill_inherit_skip":
        return f"- 跳过 Skill 继承: {data.get('reason')} prev={data.get('previous_trace_id')} current={data.get('current_trace_id')}"
    if event_type == "done":
        return f"\n### 回答\n\n{_mask_secret(str(data or '')).strip()}\n"
    if event_type == "session_memory":
        return None
    if event_type == "segment_upsert":
        action = data.get("action") or "upsert"
        seg_type = data.get("segment_type") or "unknown"
        trace_ids = data.get("trace_ids") or []
        trace_text = ",".join(trace_ids) if trace_ids else data.get("trace_id", "unknown")
        return f"- Segment Upsert: {action} {seg_type} trace_ids={trace_text}"
    if event_type == "segment_archive":
        archived = data.get("archived") or []
        reason = data.get("reason") or data.get("transition_type") or "unknown"
        return f"- Segment 归档: {len(archived)} 个，原因={reason}"
    return None


def enabled() -> bool:
    return AGENT_DEBUG_INFO_ALL


def start_run(
    session_id: str,
    question_index: int,
    thread_id: str,
    user_query: str,
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
    run_name: str | None = None,
):
    if not enabled():
        return None
    run = DebugRun(
        session_id=session_id,
        question_index=question_index,
        thread_id=thread_id,
        user_query=user_query,
        base_dir=base_dir,
        run_name=run_name,
    )
    run.start(extra)
    return _current.set(run)


def end_run(token=None) -> None:
    if token is not None:
        _current.reset(token)


def discard_current() -> None:
    run = current()
    if run:
        run.discard()


def current() -> DebugRun | None:
    return _current.get()


def event(event_type: str, data: Any = None) -> None:
    run = current()
    if run:
        run.event(event_type, data)
        run.session_cli(event_type, data)


def write(relative_path: str, data: Any) -> None:
    run = current()
    if run:
        run.write(relative_path, data)


def write_config(data: dict[str, Any]) -> None:
    run = current()
    if run:
        run.write_config(data)


def append_session_file(filename: str, content: str) -> None:
    run = current()
    if run:
        run.append_session_file(filename, _mask_secret(content))


def record_llm(node: str, payload: dict[str, Any], response: dict[str, Any] | None = None) -> None:
    run = current()
    if run:
        run.llm(node, payload, response)


def record_tool(tool_name: str, args: dict[str, Any], raw_result: str, summary: str, meta: dict[str, Any]) -> None:
    run = current()
    if run:
        run.tool(tool_name, args, raw_result, summary, meta)

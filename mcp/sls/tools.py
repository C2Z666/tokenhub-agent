"""MCP-facing SLS tools."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from config import (
    ASSEMBLED_RESPONSE_HEAD,
    ASSEMBLED_RESPONSE_LIMIT,
    ASSEMBLED_RESPONSE_TAIL,
    DEFAULT_LIMIT,
    DEFAULT_WINDOW_SECONDS,
    GATEWAY_ERROR_MESSAGE_HEAD,
    GATEWAY_ERROR_MESSAGE_LIMIT,
    GATEWAY_ERROR_MESSAGE_TAIL,
    MAX_LIMIT,
    MAX_OVERVIEW_TIME_RANGE_DAYS,
    MAX_WINDOW_SECONDS,
    REQUEST_RESPONSE_CHUNK_LIMIT,
    TIMEZONE,
    TRACE_LOGSTORE_DEFAULT_LIMIT,
    TRACE_LOGSTORE_MAX_LIMIT,
    TRACE_PAGE_SIZE,
    TRACE_REQUEST_RESPONSE_DEFAULT_LIMIT,
    TRACE_REQUEST_RESPONSE_MAX_LIMIT,
    load_config,
)
from security import (
    extract_trace_id,
    mask_sensitive_text,
    parse_json_object,
    safe_error_detail,
    truncate_chunk,
    truncate_middle,
    truncate_text,
    validate_safe_identifier,
)
from sls.client import SLSClient
from sls.query_builder import (
    AGGREGATE_DEFAULT_LIMIT,
    AGGREGATE_MAX_LIMIT,
    OVERVIEW_DEFAULT_LIMIT,
    OVERVIEW_MAX_LIMIT,
    SUPPORTED_LOGSTORES,
    errors_query,
    gateway_usage_aggregate_query,
    gateway_usage_overview_query,
    trace_query,
    validate_logstore,
)


LOGGER = logging.getLogger("tokenhub_mcp.audit")
TZ = ZoneInfo(TIMEZONE)
GATEWAY_TIME_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?\b")

_ERROR_KEYWORDS = ("ERROR", "Exception", "error", "WARN", "FATAL", "failed", "failure")


def _has_error_signal(text: str) -> bool:
    return any(kw in text for kw in _ERROR_KEYWORDS)


def _now() -> datetime:
    return datetime.now(tz=TZ)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("T", " ")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _resolve_window(start_time: str | None, end_time: str | None) -> tuple[int, int, list[str]]:
    warnings: list[str] = []
    end_dt = _parse_time(end_time) or _now()
    start_dt = _parse_time(start_time) or (end_dt - timedelta(seconds=DEFAULT_WINDOW_SECONDS))
    if end_dt <= start_dt:
        raise ValueError("end_time must be later than start_time")
    window_seconds = int((end_dt - start_dt).total_seconds())
    if window_seconds > MAX_WINDOW_SECONDS:
        raise ValueError("time window exceeds 6 hours; please narrow start_time and end_time")
    if not start_time or not end_time:
        warnings.append("未提供完整时间窗口，默认查询最近 15 分钟。")
    return int(start_dt.timestamp()), int(end_dt.timestamp()), warnings


def _resolve_overview_window(start_time: str | None, end_time: str | None) -> tuple[int, int, list[str]]:
    warnings: list[str] = []
    end_dt = _parse_time(end_time) or _now()
    start_dt = _parse_time(start_time) or (end_dt - timedelta(seconds=DEFAULT_WINDOW_SECONDS))
    if end_dt <= start_dt:
        raise ValueError("end_time must be greater than start_time")
    if end_dt - start_dt > timedelta(days=MAX_OVERVIEW_TIME_RANGE_DAYS):
        raise ValueError("sls_query_gateway_usage_overview time range cannot exceed 180 days")
    if not start_time or not end_time:
        warnings.append("未提供完整时间窗口，默认查询最近 15 分钟。")
    return int(start_dt.timestamp()), int(end_dt.timestamp()), warnings


def _clamp_limit(limit: int | None, default: int = DEFAULT_LIMIT) -> tuple[int, list[str]]:
    warnings: list[str] = []
    if limit is None:
        return default, warnings
    if limit <= 0:
        raise ValueError("limit must be positive")
    if limit > MAX_LIMIT:
        warnings.append(f"limit 超过最大值 {MAX_LIMIT}，已自动收敛。")
        return MAX_LIMIT, warnings
    return limit, warnings


def _clamp_overview_limit(limit: int | None) -> tuple[int, list[str]]:
    warnings: list[str] = []
    if limit is None:
        return OVERVIEW_DEFAULT_LIMIT, warnings
    if limit <= 0:
        raise ValueError("limit must be positive")
    if limit > OVERVIEW_MAX_LIMIT:
        warnings.append(f"limit 超过最大值 {OVERVIEW_MAX_LIMIT}，已自动收敛。")
        return OVERVIEW_MAX_LIMIT, warnings
    return limit, warnings


def _clamp_aggregate_limit(limit: int | None) -> tuple[int, list[str]]:
    warnings: list[str] = []
    if limit is None:
        return AGGREGATE_DEFAULT_LIMIT, warnings
    if limit <= 0:
        raise ValueError("limit must be positive")
    if limit > AGGREGATE_MAX_LIMIT:
        warnings.append(f"limit 超过最大值 {AGGREGATE_MAX_LIMIT}，已自动收敛。")
        return AGGREGATE_MAX_LIMIT, warnings
    return limit, warnings


def _clamp_trace_limit(limit: int | None, default: int, maximum: int, label: str) -> tuple[int, list[str]]:
    warnings: list[str] = []
    if limit is None:
        return default, warnings
    if limit <= 0:
        raise ValueError(f"{label} must be positive")
    if limit > maximum:
        warnings.append(f"{label} 超过最大值 {maximum}，已自动收敛。")
        return maximum, warnings
    return limit, warnings


def _safe_error(tool: str, start: float, code: str, message: str, exc: Exception) -> dict[str, Any]:
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    LOGGER.warning("%s failed code=%s elapsed_ms=%s detail=%s", tool, code, elapsed_ms, safe_error_detail(exc))
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "safe_detail": safe_error_detail(exc),
        },
        "meta": {"tool": tool, "elapsed_ms": elapsed_ms, "timezone": TIMEZONE},
    }


def _fetch_trace_logs_paginated(
    client: SLSClient,
    logstore: str,
    trace_id: str,
    start_ts: int,
    end_ts: int,
    max_logs: int,
) -> tuple[list[dict[str, Any]], bool]:
    if logstore in {"gateway", "gateway_usage_log"}:
        query_limit = min(max_logs, TRACE_PAGE_SIZE)
        sql = trace_query(logstore, trace_id, query_limit)
        logs = client.query_logs(logstore, sql, start_ts, end_ts, query_limit, offset=0)
        return logs[:query_limit], len(logs) >= query_limit

    logs: list[dict[str, Any]] = []
    offset = 0
    hit_limit = False
    while len(logs) < max_logs:
        page_limit = min(TRACE_PAGE_SIZE, max_logs - len(logs))
        sql = trace_query(logstore, trace_id, page_limit, sql_offset=offset)
        batch = client.query_logs(logstore, sql, start_ts, end_ts, page_limit, offset=0)
        if not batch:
            break
        logs.extend(batch)
        if len(batch) < page_limit:
            break
        offset += len(batch)

    if len(logs) >= max_logs:
        hit_limit = True
    return logs[:max_logs], hit_limit


def _flatten_message(log: dict[str, Any]) -> dict[str, Any]:
    message = log.get("message")
    parsed = parse_json_object(message) if isinstance(message, str) else {}
    flattened = dict(log)
    for key, value in parsed.items():
        flattened.setdefault(f"message.{key}", value)
        flattened.setdefault(key, value)
    return flattened


def _event_time(logstore: str, log: dict[str, Any]) -> tuple[int, str]:
    flattened = _flatten_message(log)
    raw = flattened.get("message.timestamp") or flattened.get("timestamp")
    if raw is not None:
        try:
            number = int(float(str(raw)))
            if number > 10_000_000_000:
                number = number // 1000
            return number, str(raw)
        except ValueError:
            pass

    if logstore == "gateway" and isinstance(flattened.get("message"), str):
        match = GATEWAY_TIME_RE.search(flattened["message"])
        if match:
            microsecond = (match.group(2) or "0").ljust(6, "0")
            dt = datetime.strptime(match.group(1) + "." + microsecond, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=TZ)
            return int(dt.timestamp()), dt.isoformat()

    raw_time = flattened.get("__time__") or flattened.get("__time___0")
    if raw_time is not None:
        try:
            return int(float(str(raw_time))), str(raw_time)
        except ValueError:
            pass
    return 0, ""


def _compress_value(v: Any) -> Any:
    if isinstance(v, str) and len(v) > 100:
        return v[:80] + f"...[裁剪{len(v) - 80}字符]"
    return v


def _compress_message(msg: dict[str, Any]) -> dict[str, Any]:
    compressed: dict[str, Any] = {"role": msg.get("role", "unknown")}
    content = msg.get("content", "")
    if isinstance(content, str):
        if len(content) > 100:
            compressed["content"] = content[:80] + f"...[裁剪{len(content) - 80}字符]"
        else:
            compressed["content"] = content
    elif isinstance(content, list):
        compressed["content"] = f"[多模态内容, {len(content)}个部分]"
    else:
        compressed["content"] = str(content)[:100]
    return compressed


def _compress_dict(obj: dict[str, Any], depth: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in obj.items():
        if isinstance(value, str):
            result[key] = _compress_value(value)
        elif isinstance(value, list):
            if key == "messages" and len(value) > 0:
                compressed: list[Any] = []
                for i, item in enumerate(value):
                    if i >= 3 and i < len(value) - 1:
                        if i == 3:
                            compressed.append(f"...省略{len(value) - 4}条消息...")
                        continue
                    if isinstance(item, dict):
                        compressed.append(_compress_message(item))
                    else:
                        compressed.append(str(item)[:100])
                result[key] = compressed
            elif len(value) > 5:
                result[key] = [_compress_value(v) for v in value[:3]] + [f"...共{len(value)}个元素"]
            else:
                result[key] = [_compress_value(v) for v in value]
        elif isinstance(value, dict) and depth < 2:
            result[key] = _compress_dict(value, depth + 1)
        else:
            result[key] = value
    return result


def _extract_request_skeleton(raw: str, max_length: int) -> tuple[str, bool, str]:
    """Try to extract JSON request body skeleton, preserving keys and compressing values.

    Returns (content, truncated, content_mode).
    content_mode: "full" | "skeleton" | "truncated"
    """
    if len(raw) <= max_length:
        return raw, False, "full"

    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        text, was_truncated = truncate_text(raw, max_length)
        return text, was_truncated, "truncated"

    if not isinstance(obj, dict):
        text, was_truncated = truncate_text(raw, max_length)
        return text, was_truncated, "truncated"

    skeleton = _compress_dict(obj, depth=0)
    result = json.dumps(skeleton, ensure_ascii=False, indent=2)

    if len(result) > max_length:
        result, _ = truncate_text(result, max_length)

    return result, True, "skeleton"


def _content_for_log(logstore: str, log: dict[str, Any]) -> tuple[str, bool, str]:
    """Extract display content from a log entry.

    Returns (content, truncated, content_mode).
    content_mode: "full" | "skeleton" | "truncated" — only meaningful for request logs.
    """
    flattened = _flatten_message(log)
    log_type = str(flattened.get("message.log_type") or flattened.get("log_type") or "")
    chunk = flattened.get("message.chunk") or flattened.get("chunk")
    if chunk is not None:
        if log_type == "request":
            content, truncated, mode = _extract_request_skeleton(
                str(chunk), REQUEST_RESPONSE_CHUNK_LIMIT
            )
            return content, truncated, mode
        if log_type == "response":
            text, truncated = truncate_chunk(str(chunk), REQUEST_RESPONSE_CHUNK_LIMIT)
            return text, truncated, "truncated" if truncated else "full"
        text, truncated = truncate_text(str(chunk), REQUEST_RESPONSE_CHUNK_LIMIT)
        return text, truncated, "truncated" if truncated else "full"

    message = str(flattened.get("message") or "")
    if logstore == "gateway":
        limit = GATEWAY_ERROR_MESSAGE_LIMIT if "ERROR" in message else REQUEST_RESPONSE_CHUNK_LIMIT
        text, truncated = truncate_text(message, limit)
        return text, truncated, "truncated" if truncated else "full"
    text, truncated = truncate_text(message, REQUEST_RESPONSE_CHUNK_LIMIT)
    return text, truncated, "truncated" if truncated else "full"


def _log_to_event(logstore: str, log: dict[str, Any]) -> dict[str, Any]:
    flattened = _flatten_message(log)
    sort_ts, display_time = _event_time(logstore, flattened)
    content, truncated, content_mode = _content_for_log(logstore, flattened)
    log_type = str(flattened.get("message.log_type") or flattened.get("log_type") or "")
    trace_id = (
        flattened.get("message.trace_id")
        or flattened.get("trace_id")
        or extract_trace_id(str(flattened.get("message") or ""))
    )
    event: dict[str, Any] = {
        "time": display_time,
        "sort_ts": sort_ts,
        "logstore": logstore,
        "log_type": log_type,
        "trace_id": trace_id,
        "content": content,
        "truncated": truncated,
    }
    if content_mode != "full":
        event["content_mode"] = content_mode
    return event


def _trim_streaming_responses(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    response_indices = [index for index, event in enumerate(events) if event.get("log_type") == "response"]
    if len(response_indices) <= 10:
        return events, 0

    keep = set(response_indices[:5] + response_indices[-5:])
    omitted = len(response_indices) - len(keep)
    trimmed = [event for index, event in enumerate(events) if index not in response_indices or index in keep]
    return trimmed, omitted


def _json_object(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _sse_payloads(chunk: str) -> list[tuple[str | None, str]]:
    if "data:" not in chunk and "event:" not in chunk:
        return []

    payloads: list[tuple[str | None, str]] = []
    event_name: str | None = None
    data_lines: list[str] = []
    for raw_line in chunk.splitlines():
        line = raw_line.strip()
        if not line:
            if data_lines:
                payloads.append((event_name, "\n".join(data_lines)))
                data_lines = []
                event_name = None
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    if data_lines:
        payloads.append((event_name, "\n".join(data_lines)))
    return payloads


def _append_openai_delta(obj: dict[str, Any], text_parts: list[str], tool_calls: dict[int, dict[str, Any]]) -> bool:
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return False

    handled = False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or choice.get("message") or {}
        if not isinstance(delta, dict):
            continue

        content = delta.get("content")
        if isinstance(content, str):
            text_parts.append(content)
            handled = True

        reasoning_content = delta.get("reasoning_content")
        if isinstance(reasoning_content, str):
            text_parts.append(reasoning_content)
            handled = True

        function_call = delta.get("function_call")
        if isinstance(function_call, dict):
            target = tool_calls.setdefault(0, {"arguments": ""})
            if function_call.get("name"):
                target["name"] = function_call.get("name")
            if function_call.get("arguments"):
                target["arguments"] = target.get("arguments", "") + str(function_call.get("arguments"))
            handled = True

        raw_tool_calls = delta.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            for fallback_index, tool_call in enumerate(raw_tool_calls):
                if not isinstance(tool_call, dict):
                    continue
                index = int(tool_call.get("index", fallback_index) or fallback_index)
                target = tool_calls.setdefault(index, {"arguments": ""})
                if tool_call.get("id"):
                    target["id"] = tool_call.get("id")
                if tool_call.get("type"):
                    target["type"] = tool_call.get("type")
                function = tool_call.get("function")
                if isinstance(function, dict):
                    if function.get("name"):
                        target["name"] = function.get("name")
                    if function.get("arguments"):
                        target["arguments"] = target.get("arguments", "") + str(function.get("arguments"))
                handled = True

    return handled


def _append_anthropic_delta(
    obj: dict[str, Any],
    event_name: str | None,
    text_parts: list[str],
    tool_inputs: dict[int, dict[str, Any]],
) -> bool:
    obj_type = str(obj.get("type") or event_name or "")
    if not obj_type:
        return False

    handled = False
    if obj_type == "content_block_start" and isinstance(obj.get("content_block"), dict):
        index = int(obj.get("index", 0) or 0)
        block = obj["content_block"]
        target = tool_inputs.setdefault(index, {"partial_json": ""})
        if block.get("name"):
            target["name"] = block.get("name")
        if block.get("type"):
            target["type"] = block.get("type")
        handled = True

    delta = obj.get("delta")
    if isinstance(delta, dict):
        delta_type = delta.get("type")
        if delta_type == "text_delta" and isinstance(delta.get("text"), str):
            text_parts.append(delta["text"])
            handled = True
        elif delta_type == "input_json_delta" and isinstance(delta.get("partial_json"), str):
            index = int(obj.get("index", 0) or 0)
            target = tool_inputs.setdefault(index, {"partial_json": ""})
            target["partial_json"] = target.get("partial_json", "") + delta["partial_json"]
            handled = True

    return handled or obj_type.startswith("message_") or obj_type.startswith("content_block_")


def _assemble_response_logs(logs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not logs:
        return {
            "response": "",
            "response_format": "plain",
            "response_chunk_count": 0,
            "response_truncated": False,
            "response_time_start": "",
            "response_time_end": "",
            "omitted_raw_chunk_count": 0,
        }, None

    ordered = sorted(logs, key=lambda item: _event_time("request_response", item)[0])
    text_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    tool_inputs: dict[int, dict[str, Any]] = {}
    formats: set[str] = set()

    for log in ordered:
        flattened = _flatten_message(log)
        chunk = str(flattened.get("message.chunk") or flattened.get("chunk") or "")
        if not chunk:
            continue

        payloads = _sse_payloads(chunk)
        if payloads:
            for event_name, data in payloads:
                if data.strip() == "[DONE]":
                    continue
                obj = _json_object(data)
                if obj and _append_openai_delta(obj, text_parts, tool_calls):
                    formats.add("openai_sse")
                elif obj and _append_anthropic_delta(obj, event_name, text_parts, tool_inputs):
                    formats.add("anthropic_sse")
                else:
                    text_parts.append(data)
                    formats.add("plain")
            continue

        stripped = chunk.strip()
        if stripped == "[DONE]":
            continue
        obj = _json_object(stripped)
        if obj and _append_openai_delta(obj, text_parts, tool_calls):
            formats.add("openai_sse")
        elif obj and _append_anthropic_delta(obj, None, text_parts, tool_inputs):
            formats.add("anthropic_sse")
        else:
            text_parts.append(chunk)
            formats.add("plain")

    assembled_text = "".join(text_parts)
    if tool_calls:
        tool_lines = []
        for index in sorted(tool_calls):
            item = tool_calls[index]
            name = item.get("name") or ""
            arguments = item.get("arguments") or ""
            tool_lines.append(f"tool_call[{index}] {name}: {arguments}".strip())
        assembled_text = assembled_text + "\n\n[tool_calls]\n" + "\n".join(tool_lines)
    if tool_inputs:
        input_lines = []
        for index in sorted(tool_inputs):
            item = tool_inputs[index]
            name = item.get("name") or ""
            partial_json = item.get("partial_json") or ""
            input_lines.append(f"tool_input[{index}] {name}: {partial_json}".strip())
        assembled_text = assembled_text + "\n\n[tool_inputs]\n" + "\n".join(input_lines)

    assembled_text, truncated, _original_length, _omitted_chars = truncate_middle(
        assembled_text,
        ASSEMBLED_RESPONSE_LIMIT,
        ASSEMBLED_RESPONSE_HEAD,
        ASSEMBLED_RESPONSE_TAIL,
        "response",
    )

    first_ts, first_display = _event_time("request_response", ordered[0])
    last_ts, last_display = _event_time("request_response", ordered[-1])
    response_format = "plain"
    if len(formats) == 1:
        response_format = next(iter(formats))
    elif len(formats) > 1:
        response_format = "mixed"

    assembled = {
        "response": assembled_text,
        "response_format": response_format,
        "response_chunk_count": len(ordered),
        "response_truncated": truncated,
        "response_time_start": first_display or str(first_ts),
        "response_time_end": last_display or str(last_ts),
        "omitted_raw_chunk_count": len(ordered),
    }
    if tool_calls:
        assembled["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    if tool_inputs:
        assembled["tool_inputs"] = [tool_inputs[index] for index in sorted(tool_inputs)]

    event = {
        "time": assembled["response_time_start"],
        "sort_ts": first_ts,
        "logstore": "request_response",
        "log_type": "response_assembled",
        "trace_id": _flatten_message(ordered[0]).get("message.trace_id") or _flatten_message(ordered[0]).get("trace_id"),
        "content": assembled_text,
        "chunk_count": len(ordered),
        "format": response_format,
        "time_start": assembled["response_time_start"],
        "time_end": assembled["response_time_end"],
        "truncated": truncated,
        "omitted_raw_chunk_count": len(ordered),
    }
    return assembled, event


def _request_response_events_and_assembled(logs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    response_logs = []
    normal_logs = []
    for log in logs:
        flattened = _flatten_message(log)
        log_type = str(flattened.get("message.log_type") or flattened.get("log_type") or "")
        if log_type == "response":
            response_logs.append(log)
        else:
            normal_logs.append(log)

    events = [_log_to_event("request_response", log) for log in normal_logs]
    assembled, response_event = _assemble_response_logs(response_logs)
    if response_event:
        events.append(response_event)
    return events, assembled


def _build_diagnosis_hints(events: list[dict[str, Any]], assembled: dict[str, Any]) -> dict[str, Any]:
    """Extract structured diagnostic signals from trace events.

    Scans gateway, gateway_usage_log, and request_response events for:
    - Exception class names and HTTP status codes (gateway)
    - Auth status and tokens-zero flag (gateway_usage_log)
    - Upstream error presence and response availability (request_response / assembled)
    """
    hints: dict[str, Any] = {}

    # --- gateway events ---
    exception_classes: list[str] = []
    http_status_codes: list[str] = []
    error_preview: str | None = None

    for ev in events:
        if ev.get("logstore") != "gateway":
            continue
        content = str(ev.get("content", ""))
        # Extract exception class names (e.g., ReadTimeoutException, PrematureCloseException)
        for m in re.finditer(r"\b(\w+(?:Exception|Error|Fault))\b", content):
            cls = m.group(1)
            if cls not in exception_classes:
                exception_classes.append(cls)
        # Extract HTTP status codes from gateway logs
        for m in re.finditer(r"\bHTTP[/ ]\d\.\d\s+(\d{3})\b", content):
            code = m.group(1)
            if code not in http_status_codes:
                http_status_codes.append(code)
        # Also match standalone status patterns like "status=503"
        for m in re.finditer(r"\bstatus[=:]\s*(\d{3})\b", content):
            code = m.group(1)
            if code not in http_status_codes:
                http_status_codes.append(code)
        # Capture first error preview
        if not error_preview and _has_error_signal(content):
            error_preview = content[:300]

    if exception_classes:
        hints["exception_classes"] = exception_classes
    if http_status_codes:
        hints["http_status_codes"] = http_status_codes
    if error_preview:
        hints["error_preview"] = error_preview
    # Check for stacktrace presence
    has_stacktrace = any(
        "at " in str(ev.get("content", "")) and "\n" in str(ev.get("content", ""))
        for ev in events if ev.get("logstore") == "gateway"
    )
    if has_stacktrace:
        hints["has_error_stacktrace"] = True

    # Determine error source
    if exception_classes:
        gateway_exceptions = {"ReadTimeoutException", "PrematureCloseException", "ConnectTimeoutException"}
        if any(cls in gateway_exceptions for cls in exception_classes):
            hints["error_source"] = "gateway_or_network"
        else:
            hints["error_source"] = "upstream_or_unknown"

    # Extract upstream error response body (WARN Upstream error response: ... body={...})
    for ev in events:
        if ev.get("logstore") != "gateway":
            continue
        content = str(ev.get("content", ""))
        upstream_match = re.search(
            r"Upstream error response:.*?body=(\{.*\})", content, re.DOTALL
        )
        if upstream_match:
            hints["error_source"] = "upstream"
            try:
                body = json.loads(upstream_match.group(1))
                error_obj = body.get("error", {})
                if isinstance(error_obj, dict):
                    if error_obj.get("message"):
                        hints["upstream_error_message"] = str(error_obj["message"])[:300]
                    if error_obj.get("type"):
                        hints["upstream_error_type"] = str(error_obj["type"])
                    if error_obj.get("code"):
                        hints["upstream_error_code"] = str(error_obj["code"])
            except (json.JSONDecodeError, ValueError, AttributeError):
                raw = upstream_match.group(1)[:300]
                hints["upstream_error_body_raw"] = raw
            break

    # --- gateway_usage_log events ---
    for ev in events:
        if ev.get("logstore") != "gateway_usage_log":
            continue
        content = str(ev.get("content", ""))
        # Parse structured fields from usage log
        parsed = parse_json_object(content)
        if parsed:
            # Auth status
            auth_key = parsed.get("api_key_masked") or parsed.get("api_key")
            if auth_key:
                hints["auth_passed"] = True
            # Tokens zero check
            input_tokens = parsed.get("input_tokens", -1)
            output_tokens = parsed.get("output_tokens", -1)
            try:
                if int(input_tokens) == 0 and int(output_tokens) == 0:
                    hints["tokens_zero"] = True
            except (ValueError, TypeError):
                pass

    # --- request_response / assembled ---
    has_response = bool(assembled.get("response", "").strip())
    hints["has_response"] = has_response

    # Check for upstream error in response content.
    # Exclude false positives like '"error": null' / '"error":null' which
    # appear in successful SSE responses (e.g. OpenAI responses API).
    resp_content = assembled.get("response", "")
    if resp_content:
        # Strip out benign "error": null patterns before checking
        cleaned = re.sub(r'"error"\s*:\s*null', '', resp_content)
        if _has_error_signal(cleaned):
            hints["has_upstream_error"] = True

    return hints


def _build_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    combined = "\n".join(str(event.get("content") or "") for event in events)
    model_match = re.search(r"(?i)\bModel:\s*([^,\s]+)", combined)
    if model_match:
        summary["model"] = mask_sensitive_text(model_match.group(1))
    provider_match = re.search(r"(?i)\bProvider:\s*([^,\s]+)", combined)
    if provider_match:
        summary["provider"] = mask_sensitive_text(provider_match.group(1))
    actual_match = re.search(r"(?i)\bActual Model:\s*([^,\s]+)", combined)
    if actual_match:
        summary["actual_model"] = mask_sensitive_text(actual_match.group(1))
    latency_match = re.search(r"(?i)\b(?:latency|latency_ms)[:=]\s*(\d+)", combined)
    if latency_match:
        summary["latency_ms"] = int(latency_match.group(1))
    # Determine status: strip benign '"error": null' from response content
    # before checking, to avoid false positives from successful SSE responses.
    cleaned_combined = re.sub(r'"error"\s*:\s*null', '', combined)
    if re.search(r"(?i)\b(error|exception|failed|failure)\b", cleaned_combined):
        summary["status"] = "error"
    elif events:
        summary["status"] = "success"
    return summary


def _sanitize_overview_record(record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    truncated = False
    allowed_fields = [
        "trace_id",
        "api_key_masked",
        "provider",
        "model",
        "request_time",
        "request_user_agent",
        "client_ip",
        "host",
        "base_path",
        "fc_request_id",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_output_tokens",
        "latency_ms",
        "status",
        "error_detail",
    ]
    sanitized: dict[str, Any] = {}
    for field in allowed_fields:
        value = record.get(field)
        if value is None:
            sanitized[field] = None
            continue
        if field == "error_detail":
            text, was_truncated = truncate_text(str(value), REQUEST_RESPONSE_CHUNK_LIMIT)
            sanitized[field] = text
            truncated = truncated or was_truncated
        elif isinstance(value, str):
            sanitized[field] = mask_sensitive_text(value)
        else:
            sanitized[field] = value
    return sanitized, truncated


def _sanitize_aggregate_record(record: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = [
        "time_bucket",
        "api_key_masked",
        "provider",
        "model",
        "status",
        "user_agent",
        "client_ip",
        "host",
        "base_path",
        "fc_request_id",
        "request_count",
        "success_count",
        "failed_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_output_tokens",
        "avg_latency_ms",
        "max_latency_ms",
    ]
    sanitized: dict[str, Any] = {}
    for field in allowed_fields:
        value = record.get(field)
        if value is None:
            sanitized[field] = None
        elif isinstance(value, str):
            sanitized[field] = mask_sensitive_text(value)
        else:
            sanitized[field] = value
    return sanitized


def sls_list_logstores() -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "logstores": [
                {"name": name, "description": info["description"], "primary_fields": info["primary_fields"]}
                for name, info in SUPPORTED_LOGSTORES.items()
            ]
        },
        "meta": {"tool": "sls_list_logstores", "timezone": TIMEZONE},
        "warnings": [],
    }

def sls_search_errors(
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = 1000,
    reason: str = "",
) -> dict[str, Any]:
    start = time.perf_counter()
    tool = "sls_search_errors"
    try:
        limit, warnings = _clamp_limit(limit, default=1000)
        start_ts, end_ts, window_warnings = _resolve_window(start_time, end_time)
        warnings.extend(window_warnings)
        sql = errors_query(limit)
        logs = SLSClient(load_config().sls).query_logs("gateway", sql, start_ts, end_ts, limit)
        errors = []
        truncated_any = False
        for log in logs:
            message, truncated, original_length, omitted_chars = truncate_middle(
                str(log.get("message") or ""),
                GATEWAY_ERROR_MESSAGE_LIMIT,
                GATEWAY_ERROR_MESSAGE_HEAD,
                GATEWAY_ERROR_MESSAGE_TAIL,
                "error stack",
            )
            truncated_any = truncated_any or truncated
            errors.append(
                {
                    "trace_id": extract_trace_id(str(log.get("message") or "")),
                    "message": message,
                    "__topic__": mask_sensitive_text(str(log.get("__topic__") or "")),
                    "__time__": str(log.get("__time__") or log.get("__time___0") or ""),
                    "truncated": truncated,
                    "original_length": original_length,
                    "omitted_chars": omitted_chars,
                }
            )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        LOGGER.info("%s ok count=%s limit=%s elapsed_ms=%s reason=%s", tool, len(errors), limit, elapsed_ms, reason[:120])
        return {
            "ok": True,
            "data": {"errors": errors, "count": len(errors)},
            "meta": {
                "tool": tool,
                "elapsed_ms": elapsed_ms,
                "limit": limit,
                "timezone": TIMEZONE,
                "truncated": truncated_any,
            },
            "warnings": warnings,
        }
    except Exception as exc:
        return _safe_error(tool, start, "SLS_QUERY_FAILED", "SLS 异常日志查询失败，请检查条件或配置", exc)


def sls_get_trace(
    trace_id: str,
    start_time: str | None = None,
    end_time: str | None = None,
    include_request_response: bool = True,
    max_logs_per_logstore: int = TRACE_LOGSTORE_DEFAULT_LIMIT,
    max_request_response_logs: int = TRACE_REQUEST_RESPONSE_DEFAULT_LIMIT,
    reason: str = "",
) -> dict[str, Any]:
    start = time.perf_counter()
    tool = "sls_get_trace"
    try:
        validate_safe_identifier(trace_id, "trace_id")
        start_ts, end_ts, warnings = _resolve_window(start_time, end_time)
        max_logs_per_logstore, limit_warnings = _clamp_trace_limit(
            max_logs_per_logstore,
            TRACE_LOGSTORE_DEFAULT_LIMIT,
            TRACE_LOGSTORE_MAX_LIMIT,
            "max_logs_per_logstore",
        )
        warnings.extend(limit_warnings)
        max_request_response_logs, limit_warnings = _clamp_trace_limit(
            max_request_response_logs,
            TRACE_REQUEST_RESPONSE_DEFAULT_LIMIT,
            TRACE_REQUEST_RESPONSE_MAX_LIMIT,
            "max_request_response_logs",
        )
        warnings.extend(limit_warnings)
        client = SLSClient(load_config().sls)
        query_plan = ["gateway", "gateway_usage_log"]
        if include_request_response:
            query_plan.append("request_response")

        events: list[dict[str, Any]] = []
        assembled: dict[str, Any] = {}
        log_counts = {"gateway": 0, "gateway_usage_log": 0, "request_response": 0}
        hit_log_limit = False
        for logstore in query_plan:
            max_logs = max_request_response_logs if logstore == "request_response" else max_logs_per_logstore
            logs, hit_limit = _fetch_trace_logs_paginated(client, logstore, trace_id, start_ts, end_ts, max_logs)
            log_counts[logstore] = len(logs)
            hit_log_limit = hit_log_limit or hit_limit
            if logstore == "request_response":
                request_response_events, request_response_assembled = _request_response_events_and_assembled(logs)
                events.extend(request_response_events)
                assembled.update(request_response_assembled)
            else:
                events.extend(_log_to_event(logstore, log) for log in logs)

        events.sort(key=lambda event: event.get("sort_ts") or 0)
        events, omitted_response_count = _trim_streaming_responses(events)
        truncated_any = any(bool(event.get("truncated")) for event in events) or bool(assembled.get("response_truncated"))
        for event in events:
            event.pop("sort_ts", None)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        LOGGER.info(
            "%s ok trace_id=%s count=%s omitted_response_count=%s elapsed_ms=%s reason=%s",
            tool,
            trace_id,
            len(events),
            omitted_response_count,
            elapsed_ms,
            reason[:120],
        )
        return {
            "ok": True,
            "data": {
                "trace_id": trace_id,
                "summary": _build_summary(events),
                "diagnosis_hints": _build_diagnosis_hints(events, assembled),
                "events": events,
                "assembled": assembled,
                "omitted_response_count": omitted_response_count,
            },
            "meta": {
                "tool": tool,
                "elapsed_ms": elapsed_ms,
                "timezone": TIMEZONE,
                "truncated": truncated_any,
                "fetched_log_count": sum(log_counts.values()),
                "gateway_log_count": log_counts["gateway"],
                "gateway_usage_log_count": log_counts["gateway_usage_log"],
                "request_response_log_count": log_counts["request_response"],
                "response_chunk_count": assembled.get("response_chunk_count", 0),
                "hit_log_limit": hit_log_limit,
                "possibly_incomplete": hit_log_limit,
                "max_logs_per_logstore": max_logs_per_logstore,
                "max_request_response_logs": max_request_response_logs,
            },
            "warnings": warnings,
        }
    except Exception as exc:
        return _safe_error(tool, start, "SLS_QUERY_FAILED", "SLS trace 查询失败，请检查 trace_id、时间窗口或配置", exc)


def sls_get_request_response(
    trace_id: str,
    start_time: str | None = None,
    end_time: str | None = None,
    max_logs: int = TRACE_REQUEST_RESPONSE_DEFAULT_LIMIT,
    reason: str = "",
) -> dict[str, Any]:
    start = time.perf_counter()
    tool = "sls_get_request_response"
    try:
        validate_safe_identifier(trace_id, "trace_id")
        start_ts, end_ts, warnings = _resolve_window(start_time, end_time)
        max_logs, limit_warnings = _clamp_trace_limit(
            max_logs,
            TRACE_REQUEST_RESPONSE_DEFAULT_LIMIT,
            TRACE_REQUEST_RESPONSE_MAX_LIMIT,
            "max_logs",
        )
        warnings.extend(limit_warnings)
        logs, hit_log_limit = _fetch_trace_logs_paginated(
            SLSClient(load_config().sls),
            "request_response",
            trace_id,
            start_ts,
            end_ts,
            max_logs,
        )
        events, assembled = _request_response_events_and_assembled(logs)
        events.sort(key=lambda event: event.get("sort_ts") or 0)
        omitted_response_count = 0
        truncated_any = any(bool(event.get("truncated")) for event in events) or bool(assembled.get("response_truncated"))
        for event in events:
            event.pop("sort_ts", None)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        LOGGER.info(
            "%s ok trace_id=%s count=%s omitted_response_count=%s elapsed_ms=%s reason=%s",
            tool,
            trace_id,
            len(events),
            omitted_response_count,
            elapsed_ms,
            reason[:120],
        )
        return {
            "ok": True,
            "data": {
                "trace_id": trace_id,
                "events": events,
                "count": len(events),
                "assembled": assembled,
                "omitted_response_count": omitted_response_count,
            },
            "meta": {
                "tool": tool,
                "elapsed_ms": elapsed_ms,
                "timezone": TIMEZONE,
                "truncated": truncated_any,
                "fetched_log_count": len(logs),
                "request_response_log_count": len(logs),
                "response_chunk_count": assembled.get("response_chunk_count", 0),
                "hit_log_limit": hit_log_limit,
                "possibly_incomplete": hit_log_limit,
                "max_logs": max_logs,
            },
            "warnings": warnings,
        }
    except Exception as exc:
        return _safe_error(tool, start, "SLS_QUERY_FAILED", "SLS 请求响应日志查询失败，请检查 trace_id、时间窗口或配置", exc)


def sls_query_gateway_usage_overview(
    start_time: str | None = None,
    end_time: str | None = None,
    filters: list[dict[str, Any]] | None = None,
    limit: int = OVERVIEW_DEFAULT_LIMIT,
    reason: str = "",
) -> dict[str, Any]:
    """Query TokenHub gateway usage overview with structured whitelist filters.

    Use trace_id eq for one complete trace_id, or pass a list value for multiple
    complete trace_id values in the overview table. Use trace_id contains for a
    trace_id fragment, or pass a list value for multiple fragments. If the user
    wants the complete trace chain, prefer sls_get_trace. Multiple filters are
    ANDed. Free SQL is not accepted. The start_time to end_time range cannot
    exceed 180 days.

    当用户说“过滤成功请求”“只看成功的”时，使用
    filters=[{"field":"status","op":"eq","value":"success"}]。
    当用户说“过滤失败请求”“只看失败的”“失败/进行中的”时，使用
    filters=[{"field":"status","op":"eq","value":"failed"}]。
    当用户说“API Key 前缀是 th-944”时，使用
    filters=[{"field":"api_key","op":"prefix","value":"th-944"}]。
    当用户说“模型包含 deepseek”时，使用
    filters=[{"field":"model","op":"contains","value":"deepseek"}]。
    当用户说“Provider 是 xxx”时，使用
    filters=[{"field":"provider","op":"eq","value":"xxx"}]。
    当用户说“错误信息包含 rate limit / timeout / 400”等时，使用
    filters=[{"field":"error_detail","op":"contains","value":"rate limit"}]。
    当用户提供多个完整 trace_id 时，使用
    filters=[{"field":"trace_id","op":"eq","value":["trace1","trace2"]}]，
    工具会按多条 trace_id 返回多条 overview 记录，并保持 data.records 格式。
    多个过滤条件放入 filters 数组，条件之间为 AND；本工具不接受自由 SQL。
    """

    start = time.perf_counter()
    tool = "sls_query_gateway_usage_overview"
    try:
        limit, warnings = _clamp_overview_limit(limit)
        start_ts, end_ts, window_warnings = _resolve_overview_window(start_time, end_time)
        warnings.extend(window_warnings)
        safe_filters = filters or []
        sql = gateway_usage_overview_query(safe_filters, limit)
        logs = SLSClient(load_config().sls).query_logs("gateway_usage_log", sql, start_ts, end_ts, limit)

        records: list[dict[str, Any]] = []
        truncated_any = False
        for log in logs:
            record, truncated = _sanitize_overview_record(log)
            records.append(record)
            truncated_any = truncated_any or truncated

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        LOGGER.info(
            "%s ok count=%s limit=%s elapsed_ms=%s reason=%s",
            tool,
            len(records),
            limit,
            elapsed_ms,
            reason[:120],
        )
        return {
            "ok": True,
            "data": {"records": records, "count": len(records)},
            "meta": {
                "tool": tool,
                "elapsed_ms": elapsed_ms,
                "timezone": TIMEZONE,
                "limit": limit,
                "filters": safe_filters,
                "truncated": truncated_any,
            },
            "warnings": warnings,
        }
    except ValueError as exc:
        return _safe_error(tool, start, "INVALID_FILTER", "不支持的过滤字段、操作符或查询参数", exc)
    except Exception as exc:
        return _safe_error(tool, start, "SLS_QUERY_FAILED", "SLS 网关概览查询失败，请检查时间窗口、过滤条件或配置", exc)


def sls_aggregate_gateway_usage(
    start_time: str | None = None,
    end_time: str | None = None,
    filters: list[dict[str, Any]] | None = None,
    interval: str = "hour",
    group_by: list[str] | None = None,
    limit: int = AGGREGATE_DEFAULT_LIMIT,
    reason: str = "",
) -> dict[str, Any]:
    """Aggregate TokenHub gateway usage counts by time bucket and optional dimensions.

    Use this tool for statistics questions such as "昨天每小时请求量是多少",
    "按模型统计请求数", "按 provider/status 分组统计失败数". The request_count
    is calculated after grouping by unique trace_id, because trace_id is unique
    for one request. interval supports minute/hour/day. group_by supports model,
    provider, status, api_key, user_agent, client_ip, host, base_path, fc_request_id.
    filters uses the same whitelist format as sls_query_gateway_usage_overview.
    Free SQL is not accepted. The start_time to end_time range cannot exceed 180 days.
    """

    start = time.perf_counter()
    tool = "sls_aggregate_gateway_usage"
    try:
        limit, warnings = _clamp_aggregate_limit(limit)
        start_ts, end_ts, window_warnings = _resolve_overview_window(start_time, end_time)
        warnings.extend(window_warnings)
        safe_filters = filters or []
        safe_group_by = group_by or []
        sql = gateway_usage_aggregate_query(safe_filters, interval, safe_group_by, limit)
        logs = SLSClient(load_config().sls).query_logs("gateway_usage_log", sql, start_ts, end_ts, limit)
        records = [_sanitize_aggregate_record(log) for log in logs]

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        LOGGER.info(
            "%s ok count=%s interval=%s group_by=%s limit=%s elapsed_ms=%s reason=%s",
            tool,
            len(records),
            interval,
            safe_group_by,
            limit,
            elapsed_ms,
            reason[:120],
        )
        return {
            "ok": True,
            "data": {"records": records, "count": len(records)},
            "meta": {
                "tool": tool,
                "elapsed_ms": elapsed_ms,
                "timezone": TIMEZONE,
                "interval": interval,
                "group_by": safe_group_by,
                "limit": limit,
                "filters": safe_filters,
            },
            "warnings": warnings,
        }
    except ValueError as exc:
        return _safe_error(tool, start, "INVALID_AGGREGATE_QUERY", "不支持的聚合字段、时间粒度、过滤条件或查询参数", exc)
    except Exception as exc:
        return _safe_error(tool, start, "SLS_QUERY_FAILED", "SLS 网关聚合统计查询失败，请检查时间窗口、聚合字段、过滤条件或配置", exc)

"""Intake node: extract structured entities from user query."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from agent.llm.registry import get_client
from agent.prompts import load
from agent.state import AgentState, IntakeFacts

MAX_TRACE_IDS = 20 # 最多排查20个trace_id
TRACE_ID_RE = re.compile(r"\b[a-f0-9]{32}\b", re.IGNORECASE)


def _current_time_str() -> str:
    """Return current time in +08:00."""
    from datetime import timezone
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def _default_time_window() -> tuple[str, str]:
    """Default 15 min window."""
    from datetime import timezone
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    start = now - timedelta(minutes=15)
    return start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_trace_ids(
    trace_id: str | None,
    trace_ids: list[str] | str | None,
) -> tuple[str | None, list[str]]:
    normalized: list[str] = []
    seen: set[str] = set()
    candidates = []
    if trace_id:
        candidates.append(trace_id)
    if isinstance(trace_ids, str):
        candidates.append(trace_ids)
    else:
        candidates.extend(trace_ids or [])
    for item in candidates:
        if not isinstance(item, str):
            continue
        value = item.strip().lower()
        if not TRACE_ID_RE.fullmatch(value) or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    normalized = normalized[:MAX_TRACE_IDS]
    return (normalized[0] if normalized else None), normalized


def _try_parse_json(text: str) -> dict | None:
    """Extract JSON from LLM output (may be wrapped in markdown code block)."""
    # Try to find JSON block
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    payload = m.group(1) if m else text
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None


def run_intake(state: AgentState, session_context: str = "") -> AgentState:
    """Use LLM to extract entities from user query.

    Args:
        state: Current agent state with user_query.
        session_context: Optional multi-turn context string injected
                         before the user message to help LLM understand follow-ups.
    """
    llm = get_client("intake")
    common_prompt = load("common", current_time=_current_time_str())
    intake_prompt = load("intake")
    system = f"{common_prompt}\n\n{intake_prompt}"

    # Build user message: prepend session context for multi-turn
    user_content = state.user_query
    if session_context:
        user_content = f"{session_context}\n\n当前用户输入: {state.user_query}"

    response = llm.invoke(
        messages=[{"role": "user", "content": user_content}],
        system=system,
    )

    parsed = _try_parse_json(response.text)
    if parsed:
        trace_id, trace_ids = _normalize_trace_ids(
            parsed.get("trace_id"),
            parsed.get("trace_ids") or [],
        )
        facts = IntakeFacts(
            intent=parsed.get("intent", "troubleshoot"),
            trace_id=trace_id,
            trace_ids=trace_ids,
            api_key_prefix=parsed.get("api_key_prefix"),
            username=parsed.get("username"),
            model=parsed.get("model"),
            provider=parsed.get("provider"),
            time_start=parsed.get("time_start"),
            time_end=parsed.get("time_end"),
            error_keywords=parsed.get("error_keywords") or [],
            raw_query=state.user_query,
        )
    else:
        # Fallback: minimal extraction
        facts = IntakeFacts(raw_query=state.user_query)
        trace_id, trace_ids = _normalize_trace_ids(
            None,
            TRACE_ID_RE.findall(state.user_query),
        )
        facts.trace_id = trace_id
        facts.trace_ids = trace_ids

    # Default time window if not extracted
    if not facts.time_start or not facts.time_end:
        facts.time_start, facts.time_end = _default_time_window()

    state.facts = facts
    return state

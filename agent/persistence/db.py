"""Persistence helpers: thread/message/tool_call/report CRUD."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from agent.persistence.models import (
    Message,
    Report,
    SessionSnapshot,
    Thread,
    ToolCallRecord,
    init_db,
    session,
)

__all__ = [
    "init_db",
    "new_thread",
    "finalize_thread",
    "add_message",
    "add_tool_call",
    "args_hash",
    "save_report",
    "save_session_snapshot",
    "load_latest_session_snapshot",
    "load_session_snapshots",
]


def new_thread(user_query: str) -> str:
    """Create a new thread and return its id."""
    tid = uuid.uuid4().hex
    with session() as s:
        s.add(Thread(id=tid, user_query=user_query, status="running"))
        s.commit()
    return tid


def finalize_thread(thread_id: str, status: str, iterations: int, skills_hit: list | None) -> None:
    with session() as s:
        t = s.get(Thread, thread_id)
        if t is None:
            return
        t.status = status
        t.iterations = iterations
        t.skills_hit = skills_hit
        t.finished_at = datetime.utcnow()
        s.commit()


def add_message(thread_id: str, role: str, content: str, node: str | None = None) -> None:
    with session() as s:
        s.add(Message(thread_id=thread_id, role=role, content=content, node=node))
        s.commit()


def args_hash(tool: str, args: dict[str, Any]) -> str:
    """Stable hash for (tool, args) dedup."""
    payload = json.dumps({"t": tool, "a": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def add_tool_call(
    thread_id: str,
    tool: str,
    args: dict,
    result_size: int,
    truncated: bool,
    latency_ms: int,
    error: str | None = None,
) -> None:
    with session() as s:
        s.add(ToolCallRecord(
            thread_id=thread_id,
            tool=tool,
            args_json=args,
            args_hash=args_hash(tool, args),
            result_size=result_size,
            truncated=truncated,
            latency_ms=latency_ms,
            error=error,
        ))
        s.commit()


def save_report(
    thread_id: str,
    full_report: str,
    conclusion: str | None = None,
    evidence: list | None = None,
    recommendations: str | None = None,
    skills_hit: list | None = None,
) -> None:
    with session() as s:
        s.add(Report(
            thread_id=thread_id,
            full_report=full_report,
            conclusion=conclusion,
            evidence_json=evidence,
            recommendations=recommendations,
            skills_hit=skills_hit,
        ))
        s.commit()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, set):
        return sorted(_jsonable(item) for item in value)
    return value


def _extract_conclusion(report: str | None) -> str:
    match = re.search(r"##\s*结论\s*\n(.*?)(?:\n##|\Z)", report or "", re.DOTALL)
    if match:
        return match.group(1).strip()[:1000]
    return (report or "")[:500]


def _serialize_evidence(evidence: list | None) -> list:
    items = []
    for entry in evidence or []:
        tool = getattr(entry, "tool", None)
        if tool == "_verifier_hint":
            continue
        items.append({
            "tool": tool,
            "args": _jsonable(getattr(entry, "args", {})),
            "summary": getattr(entry, "summary", "") or "",
            "trace_id": getattr(entry, "trace_id", None),
            "truncated": bool(getattr(entry, "truncated", False)),
            "hit_log_limit": bool(getattr(entry, "hit_log_limit", False)),
            "latency_ms": int(getattr(entry, "latency_ms", 0) or 0),
            "result_size": int(getattr(entry, "result_size", 0) or 0),
        })
    return items


def save_session_snapshot(session_state, state) -> None:
    """保存 chat 每轮结束后的结构化快照，用于 resume。"""
    snapshot = SessionSnapshot(
        id=uuid.uuid4().hex,
        session_id=session_state.session_id,
        thread_id=state.thread_id,
        turn_index=session_state.turn_count,
        facts_json=_jsonable(state.facts),
        evidence_json=_serialize_evidence(state.evidence),
        skills=list(state.skills or []),
        report=state.report,
        global_plan=state.global_plan,
        user_query=state.user_query,
        conclusion=_extract_conclusion(state.report),
        conversation_history_json=_jsonable(session_state.conversation_history),
        accumulated_evidence_json=_jsonable(session_state.accumulated_evidence),
        session_memory_json=_jsonable(session_state.session_memory),
    )
    with session() as s:
        s.add(snapshot)
        s.commit()


def _snapshot_to_dict(snapshot: SessionSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "session_id": snapshot.session_id,
        "thread_id": snapshot.thread_id,
        "turn_index": snapshot.turn_index,
        "facts": snapshot.facts_json or {},
        "evidence": snapshot.evidence_json or [],
        "skills": snapshot.skills or [],
        "report": snapshot.report or "",
        "global_plan": snapshot.global_plan or "",
        "user_query": snapshot.user_query or "",
        "conclusion": snapshot.conclusion or "",
        "conversation_history": snapshot.conversation_history_json or [],
        "accumulated_evidence": snapshot.accumulated_evidence_json or [],
        "session_memory": snapshot.session_memory_json or {},
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else "",
    }


def load_latest_session_snapshot(session_id: str) -> dict[str, Any] | None:
    """加载某个 chat session 的最新快照。"""
    with session() as s:
        snapshot = (
            s.query(SessionSnapshot)
            .filter_by(session_id=session_id)
            .order_by(SessionSnapshot.turn_index.desc(), SessionSnapshot.created_at.desc())
            .first()
        )
        return _snapshot_to_dict(snapshot) if snapshot else None


def load_session_snapshots(session_id: str) -> list[dict[str, Any]]:
    """加载某个 chat session 的全部快照，用于渲染历史。"""
    with session() as s:
        snapshots = (
            s.query(SessionSnapshot)
            .filter_by(session_id=session_id)
            .order_by(SessionSnapshot.turn_index.asc(), SessionSnapshot.created_at.asc())
            .all()
        )
        return [_snapshot_to_dict(snapshot) for snapshot in snapshots]

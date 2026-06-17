"""Multi-turn session state management.

Manages cross-turn context: accumulated facts, evidence summaries,
conversation history, and skills hit across a chat session.

P3.4: In-memory only (single-process chat session).
P4+:  SQLite warm storage / Redis for web service.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from agent.config import (
    SESSION_COMPRESS_AFTER,
    SESSION_HISTORY_COMPRESS_AFTER,
    SESSION_MAX_TURNS,
)
from agent.memory import SessionMemory, trace_ids_from_facts
from agent.memory.compress import compress_evidence_by_turn, compress_history_with_llm
from agent.state import Evidence, IntakeFacts

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """Cross-turn session state."""
    session_id: str = ""
    thread_ids: list[str] = field(default_factory=list)
    accumulated_facts: IntakeFacts = field(default_factory=IntakeFacts)
    accumulated_evidence: list[dict[str, Any]] = field(default_factory=list)
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    skills_hit: list[str] = field(default_factory=list)
    turn_count: int = 0
    max_turns: int = SESSION_MAX_TURNS
    session_memory: SessionMemory | None = None


class SessionManager:
    """Manages multi-turn session lifecycle."""

    def new_session(self) -> SessionState:
        session_id = uuid.uuid4().hex[:12]
        return SessionState(
            session_id=session_id,
            session_memory=SessionMemory(session_id=session_id),
        )

    def merge_facts(
        self,
        session: SessionState,
        new_facts: IntakeFacts,
    ) -> IntakeFacts:
        """Merge new turn's facts into accumulated facts.

        Strategy: new non-None values override old; lists are extended.
        """
        acc = session.accumulated_facts

        if new_facts.intent not in {"troubleshoot", "recheck"}:
            # non-investigation intents don't override accumulated facts
            return acc

        if new_facts.trace_ids:
            old_trace_ids = set(acc.trace_ids or ([acc.trace_id] if acc.trace_id else []))
            new_trace_ids = list(dict.fromkeys(new_facts.trace_ids))
            trace_changed = bool(old_trace_ids and set(new_trace_ids) - old_trace_ids)
            acc.trace_ids = new_trace_ids
            acc.trace_id = acc.trace_ids[0]
            if trace_changed:
                acc.time_start = new_facts.time_start
                acc.time_end = new_facts.time_end
        elif new_facts.trace_id:
            # B5 fix: 当 trace_id 变更时，重置时间窗口为新轮的值
            # 避免用旧 trace 的时间去查新 trace
            trace_changed = acc.trace_id and acc.trace_id != new_facts.trace_id
            acc.trace_id = new_facts.trace_id
            acc.trace_ids = [new_facts.trace_id]
            if trace_changed:
                acc.time_start = new_facts.time_start
                acc.time_end = new_facts.time_end
        if new_facts.api_key_prefix:
            acc.api_key_prefix = new_facts.api_key_prefix
        if new_facts.username:
            acc.username = new_facts.username
        if new_facts.model:
            acc.model = new_facts.model
        if new_facts.provider:
            acc.provider = new_facts.provider
        if new_facts.time_start:
            acc.time_start = new_facts.time_start
        if new_facts.time_end:
            acc.time_end = new_facts.time_end
        if new_facts.error_keywords:
            # Extend and deduplicate
            existing = set(acc.error_keywords)
            for kw in new_facts.error_keywords:
                if kw not in existing:
                    acc.error_keywords.append(kw)
                    existing.add(kw)

        acc.raw_query = new_facts.raw_query
        acc.intent = "troubleshoot"
        return acc

    def add_turn(
        self,
        session: SessionState,
        user_query: str,
        report: str,
        facts: IntakeFacts,
        evidence: list[Evidence],
        skills: list[str],
    ) -> None:
        """Record a completed turn into session state."""
        session.turn_count += 1

        # Conversation history
        session.conversation_history.append({"role": "user", "content": user_query})
        # Truncate long reports for history
        report_summary = report[:500] + "..." if len(report) > 500 else report
        session.conversation_history.append({"role": "assistant", "content": report_summary})

        # Accumulate evidence summaries (lightweight, no raw results)
        for e in evidence:
            if e.tool == "_verifier_hint":
                continue
            session.accumulated_evidence.append({
                "turn": session.turn_count,
                "tool": e.tool,
                "summary": e.summary[:300] if e.summary else "",
                "trace_id": e.trace_id or facts.trace_id,
                "trace_ids": trace_ids_from_facts(facts),
            })

        # Accumulate skills
        for s in skills:
            if s not in session.skills_hit:
                session.skills_hit.append(s)

        # Merge facts
        self.merge_facts(session, facts)

        # Compress if needed
        if session.turn_count > SESSION_COMPRESS_AFTER: # 压缩证据,不减少history 
            self._compress_old_evidence(session)
        if len(session.conversation_history) >= SESSION_HISTORY_COMPRESS_AFTER: # 压缩history,减少条数,保留最近2*SESSION_COMPRESS_AFTER条,前面的压成一条
            self._compress_history(session)

    def _compress_old_evidence(self, session: SessionState) -> None:
        """按轮压缩旧 evidence，保留关键摘要而非仅保留工具名。"""
        cutoff_turn = session.turn_count - SESSION_COMPRESS_AFTER
        old_count = sum(
            1 for e in session.accumulated_evidence
            if e.get("turn", 0) <= cutoff_turn
        )
        if old_count == 0:
            return

        session.accumulated_evidence = compress_evidence_by_turn(
            session.accumulated_evidence,
            cutoff_turn,
        )
        logger.info("Compressed %d old evidence entries by turn", old_count)

    def _compress_history(self, session: SessionState) -> None:
        """Compress conversation history with LLM and rule-based fallback."""
        if len(session.conversation_history) < SESSION_HISTORY_COMPRESS_AFTER:
            return

        old_count = len(session.conversation_history)
        session.conversation_history = compress_history_with_llm(session.conversation_history)
        logger.info(
            "Compressed conversation history from %d to %d entries",
            old_count,
            len(session.conversation_history),
        )

    def build_context_for_intake(self, session: SessionState) -> str:
        """Build a context string to inject into intake prompt for follow-up understanding."""
        if session.turn_count == 0:
            return ""

        parts: list[str] = []
        parts.append("=== 多轮会话上下文 ===")

        # Accumulated facts
        acc = session.accumulated_facts
        fact_items = []
        if acc.trace_ids:
            fact_items.append(f"trace_ids={','.join(acc.trace_ids)}")
        elif acc.trace_id:
            fact_items.append(f"trace_id={acc.trace_id}")
        if acc.model:
            fact_items.append(f"model={acc.model}")
        if acc.provider:
            fact_items.append(f"provider={acc.provider}")
        if acc.username:
            fact_items.append(f"username={acc.username}")
        if acc.time_start:
            fact_items.append(f"time={acc.time_start}~{acc.time_end}")
        if fact_items:
            parts.append(f"已知实体: {', '.join(fact_items)}")

        # Skills hit
        if session.skills_hit:
            parts.append(f"已命中 Skill: {', '.join(session.skills_hit)}")

        # Recent conversation (last 4 messages)
        recent = session.conversation_history[-4:]
        if recent:
            parts.append("近期对话:")
            for msg in recent:
                role = msg["role"]
                content = msg["content"][:200]
                parts.append(f"  [{role}] {content}")

        return "\n".join(parts)

    def is_exhausted(self, session: SessionState) -> bool:
        """Check if session has reached max turns."""
        return session.turn_count >= session.max_turns

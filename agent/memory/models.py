"""Memory data structures for investigation sessions."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.config import SESSION_MAX_BATCH_SEGMENTS, SESSION_MAX_DEEP_SEGMENTS


def make_batch_key(trace_ids: list[str]) -> str:
    """生成与 trace 顺序无关的稳定 batch key。"""
    normalized = ",".join(sorted(set(tid for tid in trace_ids if tid)))
    return hashlib.sha1(normalized.encode()).hexdigest()[:16] if normalized else ""


@dataclass
class InvestigationSegment:
    """一次 trace_id 排查的归档快照。"""

    segment_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    trace_id: str | None = None
    trace_ids: list[str] = field(default_factory=list)
    query: str = ""
    conclusion: str = ""
    root_cause: str = "unknown"
    key_evidence: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    fix_suggestion: str = ""
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at: str = ""
    segment_type: str = "deep"
    batch_key: str = ""
    source_batch_keys: list[str] = field(default_factory=list)
    source_thread_ids: list[str] = field(default_factory=list)
    root_cause_summary: str = ""
    revision_count: int = 0
    recheck_count: int = 0
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class SessionMemory:
    """当前 session 的结构化笔记。"""

    session_id: str
    batch_segments: list[InvestigationSegment] = field(default_factory=list)
    deep_segments: list[InvestigationSegment] = field(default_factory=list)
    segments: list[InvestigationSegment] = field(default_factory=list)
    current_investigation: InvestigationSegment | None = None
    active_batch_key: str = ""
    active_trace_ids: list[str] = field(default_factory=list)
    focus_trace_ids: list[str] = field(default_factory=list)
    current_state: str = ""
    discovered_issues: list[str] = field(default_factory=list)
    cross_trace_findings: str = ""
    user_corrections: list[str] = field(default_factory=list)

    def get_segment_by_trace(self, trace_id: str) -> InvestigationSegment | None:
        for segment in self.deep_segments:
            trace_ids = segment.trace_ids or ([segment.trace_id] if segment.trace_id else [])
            if segment.trace_id == trace_id or trace_id in trace_ids:
                return segment
        for segment in self.batch_segments:
            if trace_id in (segment.trace_ids or []):
                return segment
        for segment in self.segments:
            trace_ids = segment.trace_ids or ([segment.trace_id] if segment.trace_id else [])
            if segment.trace_id == trace_id or trace_id in trace_ids:
                return segment
        if self.current_investigation:
            current_trace_ids = self.current_investigation.trace_ids
            if self.current_investigation.trace_id == trace_id or trace_id in current_trace_ids:
                return self.current_investigation
        return None

    def has_active_investigation(self) -> bool:
        return self.current_investigation is not None

    def get_current_trace_ids(self) -> list[str]:
        if not self.current_investigation:
            return []
        trace_ids = list(self.current_investigation.trace_ids or [])
        if self.current_investigation.trace_id and self.current_investigation.trace_id not in trace_ids:
            trace_ids.insert(0, self.current_investigation.trace_id)
        return list(dict.fromkeys(trace_ids))

    def get_segments_summary(self, max_segments: int = 5) -> str:
        all_segments = list(self.batch_segments) + list(self.deep_segments)
        if not all_segments:
            all_segments = list(self.segments)
        if not all_segments:
            return ""

        all_segments.sort(key=lambda s: s.updated_at or s.finished_at or s.started_at, reverse=True)
        lines: list[str] = []
        for segment in all_segments[:max_segments]:
            trace_ids = segment.trace_ids or ([segment.trace_id] if segment.trace_id else [])
            evidence_summary = "; ".join(
                f"{item.get('tool', 'unknown')}: {item.get('summary', '')[:120]}"
                for item in segment.key_evidence[:3]
                if item.get("summary")
            )
            root_cause = segment.root_cause_summary if segment.segment_type == "batch" else segment.root_cause
            parts = [
                f"类型={segment.segment_type}",
                f"trace_ids={','.join(trace_ids) if trace_ids else 'unknown'}",
                f"结论={segment.conclusion[:200] or '暂无'}",
                f"根因={root_cause or 'unknown'}",
            ]
            if segment.skills:
                parts.append(f"Skill={', '.join(segment.skills)}")
            if evidence_summary:
                parts.append(f"关键证据={evidence_summary}")
            lines.append("- " + "；".join(parts))
        return "\n".join(lines)

    def evict_old_segments(self) -> None:
        """按 updated_at 淘汰超限的旧 segment，保留最近更新的。"""
        if len(self.batch_segments) > SESSION_MAX_BATCH_SEGMENTS:
            self.batch_segments.sort(
                key=lambda s: s.updated_at or s.started_at, reverse=True,
            )
            self.batch_segments = self.batch_segments[:SESSION_MAX_BATCH_SEGMENTS]

        if len(self.deep_segments) > SESSION_MAX_DEEP_SEGMENTS:
            self.deep_segments.sort(
                key=lambda s: s.updated_at or s.started_at, reverse=True,
            )
            self.deep_segments = self.deep_segments[:SESSION_MAX_DEEP_SEGMENTS]

    def archive_current(self) -> None:
        if not self.current_investigation:
            return
        if not self.current_investigation.finished_at:
            self.current_investigation.finished_at = datetime.utcnow().isoformat()
        self.segments.append(self.current_investigation)
        self.current_investigation = None

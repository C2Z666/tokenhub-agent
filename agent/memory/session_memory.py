"""SessionMemory 管理器与 trace 切换/归档逻辑。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from agent.memory.models import InvestigationSegment, SessionMemory, make_batch_key
from agent.state import AgentState, Evidence, IntakeFacts

DEEP_TOOLS = {"sls_get_trace", "sls_get_request_response", "code_explore"}
MAX_KEY_EVIDENCE = 8


@dataclass
class TraceTransition:
    """trace_id 列表变化结果。"""

    type: str  # no_change | focus | extend | partial_switch | full_switch
    archive: list[str]
    new: list[str]


def trace_ids_from_facts(facts: IntakeFacts) -> list[str]:
    """从 IntakeFacts 中获取去重保序的 trace_id 列表。"""
    trace_ids = list(facts.trace_ids or [])
    if facts.trace_id and facts.trace_id not in trace_ids:
        trace_ids.insert(0, facts.trace_id)
    return list(dict.fromkeys(tid for tid in trace_ids if tid))


def detect_trace_transition(
    prev_trace_ids: list[str],
    curr_trace_ids: list[str],
) -> TraceTransition:
    """检测 trace_id 列表变化。"""
    prev_trace_ids = list(dict.fromkeys(prev_trace_ids))
    curr_trace_ids = list(dict.fromkeys(curr_trace_ids))
    prev_set = set(prev_trace_ids)
    curr_set = set(curr_trace_ids)

    if not curr_trace_ids or not prev_trace_ids:
        return TraceTransition(type="no_change", archive=[], new=[])
    if curr_set == prev_set:
        return TraceTransition(type="no_change", archive=[], new=[])
    if curr_set <= prev_set:
        return TraceTransition(type="focus", archive=[], new=[])
    if curr_set >= prev_set:
        return TraceTransition(type="extend", archive=[], new=list(curr_set - prev_set))

    removed = list(prev_set - curr_set)
    added = list(curr_set - prev_set)
    transition_type = "full_switch" if not (prev_set & curr_set) else "partial_switch"
    return TraceTransition(type=transition_type, archive=removed, new=added)


def ensure_session_memory(session) -> SessionMemory:
    """确保 SessionState 拥有 SessionMemory。"""
    if not session.session_memory:
        session.session_memory = SessionMemory(session_id=session.session_id)
    return session.session_memory


def current_trace_ids(session_memory: SessionMemory) -> list[str]:
    current = session_memory.current_investigation
    if not current:
        return []
    trace_ids = list(current.trace_ids or [])
    if current.trace_id and current.trace_id not in trace_ids:
        trace_ids.insert(0, current.trace_id)
    return list(dict.fromkeys(trace_ids))


def build_resume_context(session_memory: SessionMemory) -> str:
    """为 resume 场景构建结构化上下文，优先展示 batch/deep 归档。"""
    if not session_memory:
        return ""

    parts: list[str] = []
    if session_memory.batch_segments:
        parts.append("## 历史批次概览")
        batches = sorted(
            session_memory.batch_segments,
            key=lambda segment: segment.updated_at or segment.started_at,
            reverse=True,
        )
        for segment in batches:
            trace_ids = segment.trace_ids or []
            trace_text = ", ".join(trace_ids[:5])
            if len(trace_ids) > 5:
                trace_text += f" 等共 {len(trace_ids)} 个"
            parts.append(
                f"- [{trace_text or 'unknown'}]：{(segment.conclusion or '暂无结论')[:300]}"
                f"（类别根因: {segment.root_cause_summary or segment.root_cause or 'unknown'}，"
                f"更新{segment.revision_count}次）"
            )

    if session_memory.deep_segments:
        parts.append("\n## 可复用深查记录")
        deep_segments = sorted(
            session_memory.deep_segments,
            key=lambda segment: segment.updated_at or segment.started_at,
            reverse=True,
        )
        for segment in deep_segments:
            trace_ids = segment.trace_ids or ([segment.trace_id] if segment.trace_id else [])
            recheck_note = f"，复查{segment.recheck_count}次" if segment.recheck_count else ""
            parts.append(
                f"- {', '.join(trace_ids) or 'unknown'}：{(segment.conclusion or '暂无结论')[:240]}"
                f"（根因: {segment.root_cause or 'unknown'}，更新{segment.revision_count}次{recheck_note}）"
            )

    if session_memory.active_trace_ids:
        focus_note = ""
        if session_memory.focus_trace_ids:
            focus_note = f"，当前聚焦: {', '.join(session_memory.focus_trace_ids)}"
        parts.append(f"\n当前主排查: {', '.join(session_memory.active_trace_ids)}{focus_note}")

    return "\n".join(parts)


def prepare_current_investigation(
    session_memory: SessionMemory,
    facts: IntakeFacts,
    query: str,
) -> TraceTransition:
    """在 intake 后准备当前排查容器，必要时归档旧 segment。"""
    curr_trace_ids = trace_ids_from_facts(facts)
    prev_trace_ids = current_trace_ids(session_memory)
    transition = detect_trace_transition(prev_trace_ids, curr_trace_ids)

    if transition.type == "full_switch":
        session_memory.archive_current()
    elif transition.type == "partial_switch":
        _archive_current_subset(session_memory, transition.archive)

    if curr_trace_ids:
        if transition.type == "focus":
            session_memory.focus_trace_ids = curr_trace_ids
        else:
            session_memory.active_trace_ids = curr_trace_ids
            session_memory.active_batch_key = make_batch_key(curr_trace_ids) if len(curr_trace_ids) > 1 else ""
            session_memory.focus_trace_ids = []

    if curr_trace_ids and (
        not session_memory.current_investigation
        or transition.type in {"full_switch", "partial_switch"}
    ):
        session_memory.current_investigation = InvestigationSegment(
            trace_id=curr_trace_ids[0] if len(curr_trace_ids) == 1 else None,
            trace_ids=curr_trace_ids,
            query=query,
        )
    elif curr_trace_ids and session_memory.current_investigation:
        if transition.type == "focus":
            merged = curr_trace_ids
        else:
            merged = list(dict.fromkeys(current_trace_ids(session_memory) + curr_trace_ids))
        session_memory.current_investigation.trace_ids = merged
        session_memory.current_investigation.trace_id = merged[0] if len(merged) == 1 else None
        if not session_memory.current_investigation.query:
            session_memory.current_investigation.query = query

    if curr_trace_ids:
        session_memory.current_state = f"正在排查 trace_id: {', '.join(curr_trace_ids)}"
    elif query:
        session_memory.current_state = f"正在处理用户问题: {query[:120]}"
    return transition


def update_current_investigation(
    session_memory: SessionMemory,
    state: AgentState,
) -> None:
    """在 reporter 完成后，将本轮结果写入当前排查容器。"""
    trace_ids = trace_ids_from_facts(state.facts)
    if not session_memory.current_investigation:
        session_memory.current_investigation = InvestigationSegment(
            trace_id=trace_ids[0] if len(trace_ids) == 1 else None,
            trace_ids=trace_ids,
            query=state.user_query,
        )

    segment = session_memory.current_investigation
    if trace_ids:
        segment.trace_ids = list(dict.fromkeys((segment.trace_ids or []) + trace_ids))
        segment.trace_id = segment.trace_ids[0] if len(segment.trace_ids) == 1 else None
    if not segment.query:
        segment.query = state.user_query
    segment.conclusion = _extract_overall_conclusion(state.report)
    segment.root_cause = _infer_root_cause(state.evidence, state.report)
    segment.fix_suggestion = _extract_fix_suggestion(state.report)
    segment.skills = list(dict.fromkeys(state.skills))
    segment.key_evidence = _build_key_evidence(state.evidence, segment.trace_ids)
    session_memory.current_state = segment.conclusion[:300] or "本轮排查已完成，等待后续问题。"
    _update_discovered_issues(session_memory, segment)

    batch_key = ""
    if len(trace_ids) > 1:
        batch_segment = upsert_batch_segment(session_memory, state, trace_ids, state.thread_id)
        batch_key = batch_segment.batch_key

    is_recheck = state.facts.intent == "recheck"
    multi_trace = len(trace_ids) > 1
    for trace_id in trace_ids:
        has_deep_evidence = any(_evidence_is_deep_trace_evidence(entry, trace_id) for entry in state.evidence)
        should_upsert_deep = not multi_trace or has_deep_evidence
        if should_upsert_deep:
            upsert_deep_segment(
                session_memory,
                trace_id,
                state,
                state.thread_id,
                batch_key=batch_key,
                is_recheck=is_recheck,
                allow_conclusion_fallback=not multi_trace,
            )

    if trace_ids:
        session_memory.active_trace_ids = trace_ids
        session_memory.active_batch_key = batch_key if len(trace_ids) > 1 else ""

    # 淘汰超限的旧 segment
    session_memory.evict_old_segments()


def upsert_batch_segment(
    session_memory: SessionMemory,
    state: AgentState,
    trace_ids: list[str],
    thread_id: str,
) -> InvestigationSegment:
    """按 batch_key 查找并更新或创建 batch segment。"""
    normalized_trace_ids = list(dict.fromkeys(tid for tid in trace_ids if tid))
    if len(normalized_trace_ids) <= 1:
        raise ValueError("batch segment requires at least two trace_ids")
    batch_key = make_batch_key(normalized_trace_ids)
    existing = _find_batch_by_key(session_memory, batch_key)
    now = datetime.utcnow().isoformat()

    conclusion = _extract_overall_conclusion(state.report)
    root_cause_summary = _infer_root_cause(state.evidence, state.report)
    key_evidence = _build_key_evidence(state.evidence, normalized_trace_ids)
    skills = list(dict.fromkeys(state.skills))

    if existing:
        existing.query = state.user_query or existing.query
        existing.conclusion = conclusion or existing.conclusion
        existing.root_cause_summary = root_cause_summary or existing.root_cause_summary
        existing.root_cause = existing.root_cause_summary or existing.root_cause
        existing.key_evidence = _merge_evidence(existing.key_evidence, key_evidence)
        existing.skills = list(dict.fromkeys(existing.skills + skills))
        if thread_id and thread_id not in existing.source_thread_ids:
            existing.source_thread_ids.append(thread_id)
        existing.updated_at = now
        existing.revision_count += 1
        _record_segment_upsert("update", existing)
        return existing

    segment = InvestigationSegment(
        segment_type="batch",
        batch_key=batch_key,
        trace_id=None,
        trace_ids=normalized_trace_ids,
        query=state.user_query,
        conclusion=conclusion,
        root_cause=root_cause_summary,
        root_cause_summary=root_cause_summary,
        key_evidence=key_evidence,
        skills=skills,
        source_thread_ids=[thread_id] if thread_id else [],
        updated_at=now,
    )
    session_memory.batch_segments.append(segment)
    _record_segment_upsert("create", segment)
    return segment


def upsert_deep_segment(
    session_memory: SessionMemory,
    trace_id: str,
    state: AgentState,
    thread_id: str,
    batch_key: str = "",
    is_recheck: bool = False,
    allow_conclusion_fallback: bool = True,
) -> InvestigationSegment:
    """按 trace_id 查找并更新或创建 deep segment。"""
    existing = _find_deep_by_trace(session_memory, trace_id)
    trace_evidence = [entry for entry in state.evidence if _evidence_belongs_to_trace(entry, trace_id)]
    conclusion = _extract_trace_conclusion(
        state.report,
        trace_id,
        allow_fallback=allow_conclusion_fallback or existing is not None,
    )
    root_cause = _infer_root_cause(trace_evidence, state.report)
    fix_suggestion = _extract_fix_suggestion(state.report)
    key_evidence = _build_key_evidence(trace_evidence, [trace_id])
    skills = list(dict.fromkeys(state.skills))
    now = datetime.utcnow().isoformat()

    if existing:
        existing.query = state.user_query or existing.query
        existing.conclusion = conclusion or existing.conclusion
        existing.root_cause = root_cause or existing.root_cause
        existing.fix_suggestion = fix_suggestion or existing.fix_suggestion
        existing.key_evidence = _merge_evidence(existing.key_evidence, key_evidence)
        existing.skills = list(dict.fromkeys(existing.skills + skills))
        if batch_key and batch_key not in existing.source_batch_keys:
            existing.source_batch_keys.append(batch_key)
        if thread_id and thread_id not in existing.source_thread_ids:
            existing.source_thread_ids.append(thread_id)
        existing.updated_at = now
        existing.revision_count += 1
        if is_recheck:
            existing.recheck_count += 1
        _record_segment_upsert("update", existing)
        return existing

    segment = InvestigationSegment(
        segment_type="deep",
        trace_id=trace_id,
        trace_ids=[trace_id],
        query=state.user_query,
        conclusion=conclusion,
        root_cause=root_cause,
        fix_suggestion=fix_suggestion,
        key_evidence=key_evidence,
        skills=skills,
        source_batch_keys=[batch_key] if batch_key else [],
        source_thread_ids=[thread_id] if thread_id else [],
        updated_at=now,
        recheck_count=1 if is_recheck else 0,
    )
    session_memory.deep_segments.append(segment)
    _record_segment_upsert("create", segment)
    return segment


def _record_segment_upsert(action: str, segment: InvestigationSegment) -> None:
    try:
        from agent.debug import info_all

        payload = {
            "time": datetime.utcnow().isoformat(),
            "action": action,
            "segment_type": segment.segment_type,
            "segment_id": segment.segment_id,
            "batch_key": segment.batch_key,
            "trace_id": segment.trace_id,
            "trace_ids": segment.trace_ids,
            "revision_count": segment.revision_count,
            "recheck_count": segment.recheck_count,
            "source_thread_ids": segment.source_thread_ids,
            "source_batch_keys": segment.source_batch_keys,
            "conclusion": segment.conclusion[:300],
            "root_cause": segment.root_cause,
            "root_cause_summary": segment.root_cause_summary,
        }
        info_all.event("segment_upsert", payload)
        content = (
            f"\n## Segment Upsert - {payload['time']} UTC\n\n"
            f"- action: {action}\n"
            f"- segment_type: {segment.segment_type}\n"
            f"- segment_id: {segment.segment_id}\n"
            f"- batch_key: {segment.batch_key}\n"
            f"- trace_ids: {segment.trace_ids}\n"
            f"- revision_count: {segment.revision_count}\n\n"
            "```json\n"
            f"{json.dumps(info_all.sanitize(payload), ensure_ascii=False, indent=2)}\n"
            "```\n"
        )
        info_all.append_session_file("segment-upsert.md", content)
    except Exception:
        pass


def _find_batch_by_key(session_memory: SessionMemory, batch_key: str) -> InvestigationSegment | None:
    for segment in session_memory.batch_segments:
        if segment.batch_key == batch_key:
            return segment
    return None


def _find_deep_by_trace(session_memory: SessionMemory, trace_id: str) -> InvestigationSegment | None:
    for segment in session_memory.deep_segments:
        trace_ids = segment.trace_ids or ([segment.trace_id] if segment.trace_id else [])
        if segment.trace_id == trace_id or trace_id in trace_ids:
            return segment
    return None


def _merge_evidence(
    old_evidence: list[dict],
    new_evidence: list[dict],
    max_items: int = MAX_KEY_EVIDENCE,
) -> list[dict]:
    """合并新旧证据，按新证据优先截取。"""
    seen_keys = set()
    merged = []
    for item in new_evidence + old_evidence:
        key = (item.get("tool", ""), item.get("trace_id", ""), item.get("summary", "")[:80])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        merged.append(item)
        if len(merged) >= max_items:
            break
    return merged


def _extract_trace_conclusion_strict(report: str, trace_id: str) -> str:
    """仅在报告明确提到 trace_id 或 short_id 时提取段落，不使用整体结论兜底。"""
    if not report or not trace_id:
        return ""
    short_id = trace_id[:8]
    paragraphs = report.split("\n\n")
    relevant = [paragraph for paragraph in paragraphs if trace_id in paragraph or short_id in paragraph]
    if relevant:
        return "\n\n".join(relevant)[:500]
    return ""


def _extract_trace_conclusion(report: str, trace_id: str, allow_fallback: bool = True) -> str:
    """从报告中提取某个 trace_id 相关的结论段落，可按需使用整体结论兜底。"""
    conclusion = _extract_trace_conclusion_strict(report, trace_id)
    if conclusion:
        return conclusion
    return _extract_overall_conclusion(report) if allow_fallback else ""


def _evidence_is_deep_trace_evidence(evidence: Evidence, trace_id: str) -> bool:
    """判断某条 evidence 是否为指定 trace_id 的深查证据。"""
    if evidence.trace_id == trace_id:
        return True
    if evidence.args.get("trace_id") == trace_id:
        return True
    return False


def _evidence_belongs_to_trace(evidence: Evidence, trace_id: str) -> bool:
    """判断某条 evidence 是否关联到指定 trace_id。"""
    if _evidence_is_deep_trace_evidence(evidence, trace_id):
        return True
    for item in evidence.args.get("filters", []) or []:
        if item.get("field") == "trace_id" and item.get("value") == trace_id:
            return True
        if item.get("field") == "trace_id" and trace_id in str(item.get("value", "")):
            return True
    try:
        trace_text = json.dumps(evidence.args, ensure_ascii=False)
        return trace_id in trace_text
    except (TypeError, ValueError):
        return False


def _archive_current_subset(session_memory: SessionMemory, trace_ids: list[str]) -> None:
    current = session_memory.current_investigation
    if not current or not trace_ids:
        return

    trace_set = set(trace_ids)
    archived_evidence = [
        item for item in current.key_evidence
        if item.get("trace_id") in trace_set or not item.get("trace_id")
    ]
    archived = InvestigationSegment(
        trace_id=trace_ids[0] if len(trace_ids) == 1 else None,
        trace_ids=list(dict.fromkeys(trace_ids)),
        query=current.query,
        conclusion=current.conclusion,
        root_cause=current.root_cause,
        key_evidence=archived_evidence,
        skills=list(current.skills),
        fix_suggestion=current.fix_suggestion,
        started_at=current.started_at,
        finished_at=datetime.utcnow().isoformat(),
    )
    session_memory.segments.append(archived)

    remaining = [tid for tid in current_trace_ids(session_memory) if tid not in trace_set]
    current.trace_ids = remaining
    current.trace_id = remaining[0] if len(remaining) == 1 else None
    current.key_evidence = [
        item for item in current.key_evidence
        if item.get("trace_id") not in trace_set
    ]
    if not remaining:
        session_memory.current_investigation = None


def _build_key_evidence(evidence: list[Evidence], trace_ids: list[str]) -> list[dict]:
    trace_set = set(trace_ids)
    items = []
    for entry in evidence:
        if entry.tool == "_verifier_hint" or not entry.summary:
            continue
        if trace_set and entry.trace_id and entry.trace_id not in trace_set:
            continue
        items.append({
            "tool": entry.tool,
            "summary": entry.summary[:300],
            "trace_id": entry.trace_id,
            "timestamp": datetime.utcnow().isoformat(),
        })
        if len(items) >= MAX_KEY_EVIDENCE:
            break
    return items


def _extract_overall_conclusion(report: str) -> str:
    match = re.search(r"##\s*结论\s*\n(.*?)(?:\n##|\Z)", report or "", re.DOTALL)
    if match:
        return match.group(1).strip()[:1200]
    return (report or "")[:1200]


def _extract_fix_suggestion(report: str) -> str:
    match = re.search(r"##\s*(?:修复建议|建议|处理建议)\s*\n(.*?)(?:\n##|\Z)", report or "", re.DOTALL)
    if match:
        return match.group(1).strip()[:500]
    return ""


def _infer_root_cause(evidence: list[Evidence], report: str) -> str:
    text = "\n".join([report or ""] + [e.summary for e in evidence if e.summary]).lower()
    if any(word in text for word in ("timeout", "超时", "timed out")):
        return "timeout"
    if any(word in text for word in ("401", "403", "unauthorized", "鉴权", "认证")):
        return "auth"
    if any(word in text for word in ("upstream", "上游", "provider")):
        return "upstream"
    if any(word in text for word in ("protocol", "协议", "http")):
        return "protocol"
    return "unknown"


def _update_discovered_issues(session_memory: SessionMemory, segment: InvestigationSegment) -> None:
    if not segment.conclusion:
        return
    item = segment.conclusion[:200]
    if item not in session_memory.discovered_issues:
        session_memory.discovered_issues.append(item)

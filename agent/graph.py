"""Phase 2 LangGraph assembly.

Pipeline: intake → skill_router → [planner ⇄ executor → verifier] → reporter

The planner→executor→verifier loop runs up to MAX_PLAN_VERIFY_LOOPS times.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, TypedDict

from agent.config import SESSION_COMPRESS_AFTER
from agent.nodes.executor import run_executor
from agent.nodes.intake import run_intake
from agent.nodes.planner import run_planner
from agent.nodes.reporter import run_reporter
from agent.nodes.skill_router import reroute_from_evidence, reroute_from_embedding, run_skill_router
from agent.nodes.verifier import run_verifier
from agent.persistence import add_message, finalize_thread, init_db, new_thread, save_session_snapshot
from agent.state import AgentState, Evidence
from agent.tools.mcp_client import call_tool, list_tools, mcp_session

logger = logging.getLogger(__name__)


def _query_explicit_trace_ids(raw_query: str) -> list[str]:
    """Return trace_ids explicitly mentioned in the current user query."""
    return list(dict.fromkeys(re.findall(r"\b[0-9a-fA-F]{32}\b", raw_query or "")))


def _is_recheck_request(facts) -> bool:
    """Whether intake classified this turn as an explicit re-investigation."""
    return facts.intent == "recheck"


def _is_exploratory_query(facts) -> bool:
    """Whether intake produced actionable troubleshooting entities."""
    return bool(
        facts.trace_id
        or facts.trace_ids
        or facts.error_keywords
        or facts.username
        or facts.api_key_prefix
        or facts.model
        or facts.provider
    )


def _classify_investigation_mode(
    *,
    original_intent: str,
    explicit_trace_ids: list[str],
    session_memory,
    facts,
) -> str:
    """Classify this turn without keyword lists.

    new: run a fresh investigation.
    summary_only: answer from short-term memory only.
    reuse_session: reuse completed same-trace investigation in this session.
    """
    if explicit_trace_ids:
        if _all_trace_ids_investigated(session_memory, explicit_trace_ids) and not _is_recheck_request(facts):
            return "reuse_session"
        return "new"
    if original_intent == "summary":
        return "summary_only"
    return "new"


def _recent_assistant_reports(session, limit: int = 4) -> str:
    """Build short memory from recent assistant reports."""
    reports = [
        m.get("content", "")
        for m in session.conversation_history
        if m.get("role") == "assistant" and m.get("content")
    ]
    recent = reports[-limit:]
    parts = []
    for i, report in enumerate(recent, 1):
        parts.append(f"前轮报告 {i}:\n{report[:800]}")
    return "\n\n".join(parts)



def _previously_investigated_context(session_memory, trace_ids: list[str]) -> str:
    """Build context for trace_ids with deep investigation results in this session."""
    if not session_memory or not trace_ids:
        return ""

    candidate_segments = list(getattr(session_memory, "deep_segments", []) or [])
    candidate_segments.extend(
        segment
        for segment in getattr(session_memory, "segments", []) or []
        if getattr(segment, "segment_type", "deep") == "deep"
    )
    current = getattr(session_memory, "current_investigation", None)
    if current and getattr(current, "segment_type", "deep") == "deep" and (
        getattr(current, "conclusion", "") or getattr(current, "key_evidence", None)
    ):
        candidate_segments.append(current)

    lines = []
    matched_trace_ids: set[str] = set()
    seen_segments = set()
    for trace_id in trace_ids:
        segment = None
        for archived in candidate_segments:
            archived_trace_ids = archived.trace_ids or ([archived.trace_id] if archived.trace_id else [])
            if archived.trace_id == trace_id or trace_id in archived_trace_ids:
                segment = archived
                break
        if not segment:
            continue
        matched_trace_ids.add(trace_id)
        if segment.segment_id in seen_segments:
            continue
        seen_segments.add(segment.segment_id)
        segment_trace_ids = segment.trace_ids or ([segment.trace_id] if segment.trace_id else [trace_id])
        evidence_summary = "; ".join(
            f"{item.get('tool', 'unknown')}: {item.get('summary', '')[:160]}"
            for item in (segment.key_evidence or [])[:3]
            if item.get("summary")
        )
        lines.append(
            f"trace_ids={', '.join(segment_trace_ids)} 本次会话已有 deep 深查记录：\n"
            f"  结论: {(segment.conclusion or '暂无结论')[:800]}\n"
            f"  根因: {segment.root_cause or 'unknown'}\n"
            f"  Skill: {', '.join(segment.skills) if segment.skills else '无'}\n"
            f"  关键证据: {evidence_summary or '暂无'}\n"
            "  处理策略: 对这些已命中 deep 的 trace_id 直接复用历史深查记录，不要重新调用查询工具。"
        )
    unmatched_trace_ids = [trace_id for trace_id in trace_ids if trace_id not in matched_trace_ids]
    if matched_trace_ids and unmatched_trace_ids:
        lines.append(
            "多 trace_id 部分命中历史 deep："
            f"已命中={', '.join(trace_id for trace_id in trace_ids if trace_id in matched_trace_ids)}；"
            f"未命中={', '.join(unmatched_trace_ids)}。"
            "Planner 只需要继续排查未命中的 trace_id，已命中的 trace_id 不要重复查询。"
        )
    elif matched_trace_ids:
        lines.append("本轮 trace_id 已全部命中历史 deep；如果不是重查意图，应直接复用，不要重新查询。")
    return "\n".join(lines)


def _all_trace_ids_investigated(session_memory, trace_ids: list[str]) -> bool:
    """Return whether all trace_ids have deep investigation results in this session."""
    if not session_memory or not trace_ids:
        return False
    investigated_ids: set[str] = set()
    deep_segments = list(getattr(session_memory, "deep_segments", []) or [])
    deep_segments.extend(
        segment
        for segment in getattr(session_memory, "segments", []) or []
        if getattr(segment, "segment_type", "deep") == "deep"
    )
    for segment in deep_segments:
        if segment.conclusion or segment.key_evidence:
            investigated_ids.update(segment.trace_ids or ([segment.trace_id] if segment.trace_id else []))
    current = getattr(session_memory, "current_investigation", None)
    if current and getattr(current, "segment_type", "deep") == "deep" and (
        getattr(current, "conclusion", "") or getattr(current, "key_evidence", None)
    ):
        investigated_ids.update(current.trace_ids or ([current.trace_id] if current.trace_id else []))
    return all(trace_id in investigated_ids for trace_id in trace_ids)



def _ensure_skills_indexed() -> None:
    """Lazily index Skill documents into RAG if not already present."""
    try:
        from agent.rag import get_store
        from agent.rag.indexer import index_skills

        store = get_store()
        if store.count({"source_type": "skill"}) == 0:
            index_skills(store)
    except Exception:
        # RAG is optional; don't block the pipeline if it fails
        import logging
        logging.getLogger(__name__).warning("RAG skill indexing failed", exc_info=True)


def _tools_for_claude(tools: list[dict]) -> list[dict]:
    """Filter out ping tool."""
    return [t for t in tools if t["name"] != "ping"]


class InvestigationGraphState(TypedDict):
    state: AgentState
    verdict: str


def _build_investigation_graph(mcp, mcp_tools: list[dict], all_tools: list[dict], emit, on_event=None):
    """Build the LangGraph loop for planner → executor → verifier."""
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(InvestigationGraphState)

    async def planner_node(graph_state: InvestigationGraphState) -> InvestigationGraphState:
        state = graph_state["state"]
        emit("loop_start", {"iteration": state.iterations + 1})
        emit("node_start", {"node": "planner"})
        state = run_planner(state, all_tools)
        for rag_entry in getattr(state, "_rag_entries", []):
            if not rag_entry.get("emitted"):
                emit("rag_enter", rag_entry)
                rag_entry["emitted"] = True
        state.iterations += 1
        emit("node_end", {"node": "planner", "plan_size": len(state.plan)})
        if state.plan:
            emit("plan_detail", {"iteration": state.iterations, "plan": state.plan})

        rag_hits = getattr(state, "_rag_hits", None)
        if rag_hits and rag_hits.get("history", 0) > 0:
            emit("rag_hit", {"type": "history", "count": rag_hits["history"]})
        if not state.plan:
            emit("no_plan", {"iteration": state.iterations})
        return {"state": state, "verdict": graph_state.get("verdict", "continue")}

    async def executor_node(graph_state: InvestigationGraphState) -> InvestigationGraphState:
        state = graph_state["state"]
        emit("node_start", {"node": "executor"})
        state = await run_executor(state, mcp, mcp_tools=mcp_tools, on_event=on_event)
        emit("node_end", {"node": "executor", "evidence_count": len(state.evidence)})
        return {"state": state, "verdict": graph_state.get("verdict", "continue")}

    async def skill_reroute_node(graph_state: InvestigationGraphState) -> InvestigationGraphState:
        state = graph_state["state"]
        if not state.skills:
            prev_skills = list(state.skills)
            state = reroute_from_evidence(state)
            if state.skills and state.skills != prev_skills:
                emit("skill_reroute", {"skills": state.skills, "level": 1})

        if not state.skills:
            prev_skills = list(state.skills)
            state = reroute_from_embedding(state)
            for rag_entry in getattr(state, "_rag_entries", []):
                if not rag_entry.get("emitted"):
                    emit("rag_enter", rag_entry)
                    rag_entry["emitted"] = True
            if state.skills and state.skills != prev_skills:
                emit("skill_reroute", {"skills": state.skills, "level": 2})
            rag_debug = getattr(state, "_skill_rag_debug", None)
            if rag_debug:
                emit("rag_hit", rag_debug)
        return {"state": state, "verdict": graph_state.get("verdict", "continue")}

    async def verifier_node(graph_state: InvestigationGraphState) -> InvestigationGraphState:
        state = graph_state["state"]
        emit("node_start", {"node": "verifier"})
        state, verdict = run_verifier(state)
        emit("node_end", {"node": "verifier", "verdict": verdict})
        if verdict != "done":
            emit("loop_continue", {"iteration": state.iterations, "reason": "verifier: continue"})
        return {"state": state, "verdict": verdict}

    def route_after_planner(graph_state: InvestigationGraphState) -> str:
        state = graph_state["state"]
        if not state.plan:
            return "done"
        return "executor"

    def route_after_verifier(graph_state: InvestigationGraphState) -> str:
        state = graph_state["state"]
        if graph_state.get("verdict") == "done" or not state.can_iterate:
            return "done"
        return "planner"

    graph.add_node("planner", planner_node)
    graph.add_node("executor", executor_node)
    graph.add_node("skill_reroute", skill_reroute_node)
    graph.add_node("verifier", verifier_node)

    graph.add_edge(START, "planner")
    graph.add_conditional_edges("planner", route_after_planner, {
        "executor": "executor",
        "done": END,
    })
    graph.add_edge("executor", "skill_reroute")
    graph.add_edge("skill_reroute", "verifier")
    graph.add_conditional_edges("verifier", route_after_verifier, {
        "planner": "planner",
        "done": END,
    })

    return graph.compile()


async def run_graph(user_query: str, on_event=None, session=None, debug_options: dict[str, Any] | None = None) -> str:
    """Run the full investigation graph.

    Args:
        user_query: Natural language question.
        on_event: Event callback for CLI progress display.
        session: Optional SessionState for multi-turn context.
                 When provided, accumulated facts/evidence/skills are
                 injected and the session is updated after the run.

    Returns the final report text.
    """
    # Import here to avoid circular imports
    from agent.memory import (
        ensure_session_memory,
        prepare_current_investigation,
        trace_ids_from_facts,
        update_current_investigation,
    )
    from agent.session import SessionManager

    session_mgr = SessionManager() if session else None
    debug_token = None

    def emit(event_type: str, data: Any = None):
        try:
            from agent.debug import info_all
            info_all.event(event_type, data)
        except Exception:
            pass
        if on_event:
            on_event(event_type, data)

    def record_session_memory_snapshot() -> None:
        if not session:
            return
        try:
            from agent.debug import info_all
            memory = ensure_session_memory(session)
            segment_archive_snapshot = {
                "time": datetime.utcnow().isoformat(),
                "question_index": question_index,
                "thread_id": thread_id,
                "user_query": user_query,
                "active_batch_key": memory.active_batch_key,
                "active_trace_ids": memory.active_trace_ids,
                "focus_trace_ids": memory.focus_trace_ids,
                "batch_segments": memory.batch_segments,
                "deep_segments": memory.deep_segments,
                "legacy_segments": memory.segments,
                "current_investigation": memory.current_investigation,
            }
            info_all.write("final/session_memory.json", session)
            info_all.write("final/segment_archive_snapshot.json", segment_archive_snapshot)
            info_all.event("session_memory", session)
        except Exception:
            pass

    def record_segment_archive(transition: Any) -> None:
        if not session or not transition or not getattr(transition, "archive", None):
            return
        try:
            from agent.debug import info_all
            memory = ensure_session_memory(session)
            archived_trace_ids = set(getattr(transition, "archive", []) or [])
            archived_segments = []
            for segment in memory.segments:
                trace_ids = segment.trace_ids or ([segment.trace_id] if segment.trace_id else [])
                if archived_trace_ids and not (set(trace_ids) & archived_trace_ids):
                    continue
                archived_segments.append(segment)

            payload = {
                "time": datetime.utcnow().isoformat(),
                "question_index": question_index,
                "thread_id": thread_id,
                "user_query": user_query,
                "transition_type": getattr(transition, "type", "unknown"),
                "reason": "trace_changed",
                "archived_trace_ids": list(archived_trace_ids),
                "new_trace_ids": getattr(transition, "new", []) or [],
                "archived": archived_segments,
            }
            emit("segment_archive", payload)
            content = (
                f"\n## Segment Archive - Turn {question_index}\n\n"
                f"- time: {payload['time']} UTC\n"
                f"- thread_id: {thread_id}\n"
                f"- reason: {payload['reason']}\n"
                f"- transition_type: {payload['transition_type']}\n"
                f"- user_query: {user_query}\n"
                f"- archived_trace_ids: {payload['archived_trace_ids']}\n"
                f"- new_trace_ids: {payload['new_trace_ids']}\n\n"
                "```json\n"
                f"{json.dumps(info_all.sanitize(payload), ensure_ascii=False, indent=2)}\n"
                "```\n"
            )
            info_all.append_session_file("segment-archive.md", content)
        except Exception:
            pass

    def compressed_evidence_count() -> int:
        return sum(
            1 for e in session.accumulated_evidence
            if isinstance(e, dict) and (e.get("compressed") or e.get("tool") == "_compressed")
        )

    def emit_session_compression(
        before_history_count: int,
        before_compressed_evidence_count: int,
    ) -> None:
        after_compressed_evidence_count = compressed_evidence_count()
        if after_compressed_evidence_count > before_compressed_evidence_count:
            emit("evidence_compress", {
                "compressed": after_compressed_evidence_count - before_compressed_evidence_count,
                "keep_recent_turns": SESSION_COMPRESS_AFTER,
            })
        after_history_count = len(session.conversation_history)
        if after_history_count < before_history_count + 2:
            emit("history_compress", {
                "before": before_history_count + 2,
                "after": after_history_count,
            })

    # Initialize persistence
    init_db()

    # Ensure RAG skills are indexed (lazy: only if empty)
    _ensure_skills_indexed()

    thread_id = new_thread(user_query)
    add_message(thread_id, "user", user_query)

    if session:
        session_id = session.session_id
        question_index = session.turn_count + 1
    else:
        session_id = thread_id
        question_index = 1
    try:
        from agent.debug import info_all
        debug_extra = {
            "mode": "chat" if session else "single",
            "session_turn_count_before": session.turn_count if session else 0,
        }
        debug_options = debug_options or {}
        debug_extra.update(debug_options.get("extra", {}))
        debug_token = info_all.start_run(
            session_id=debug_options.get("session_id", session_id),
            question_index=debug_options.get("question_index", question_index),
            thread_id=thread_id,
            user_query=user_query,
            extra=debug_extra,
            base_dir=debug_options.get("base_dir"),
            run_name=debug_options.get("run_name"),
        )
        info_all.event("turn_start", {
            "question_index": debug_options.get("question_index", question_index),
            "query": user_query,
        })
    except Exception:
        debug_token = None

    # Initialize state
    state = AgentState(thread_id=thread_id, user_query=user_query)

    # --- intake ---
    emit("node_start", {"node": "intake"})
    session_context = session_mgr.build_context_for_intake(session) if session else ""
    try:
        from agent.debug import info_all
        info_all.write_config({"session_context_for_intake": session_context})
    except Exception:
        pass
    state = run_intake(state, session_context=session_context)
    emit("node_end", {"node": "intake", "facts": {
        "intent": state.facts.intent,
        "trace_id": state.facts.trace_id,
        "trace_ids": state.facts.trace_ids,
        "model": state.facts.model,
        "provider": state.facts.provider,
        "time": f"{state.facts.time_start} ~ {state.facts.time_end}",
    }})

    # 当前轮显式 trace_id 是新排查的最高优先级，不受历史上下文继承影响
    original_intent = state.facts.intent
    explicit_trace_ids = _query_explicit_trace_ids(state.user_query)
    has_explicit_trace_id = bool(explicit_trace_ids)
    current_turn_trace_id = state.facts.trace_id if has_explicit_trace_id else None
    current_turn_trace_ids = explicit_trace_ids if has_explicit_trace_id else []
    current_turn_has_time_range = bool(state.facts.time_start or state.facts.time_end)
    previous_turn_trace_id = session.accumulated_facts.trace_id if session else None
    previous_turn_trace_ids = trace_ids_from_facts(session.accumulated_facts) if session else []

    if session:
        session_memory = ensure_session_memory(session)
        state.investigation_mode = _classify_investigation_mode(
            original_intent=original_intent,
            explicit_trace_ids=current_turn_trace_ids,
            session_memory=session_memory,
            facts=state.facts,
        )
        state.memory_summary_mode = state.investigation_mode in {"summary_only", "reuse_session"}
        if state.investigation_mode == "summary_only":
            state.facts.trace_id = None
            state.facts.trace_ids = []
            state.facts.error_keywords = []
        if state.investigation_mode == "new":
            transition = prepare_current_investigation(session_memory, state.facts, user_query)
            record_segment_archive(transition)
        else:
            transition = None
        emit("trace_transition", {
            "type": transition.type if transition else "no_change",
            "archive": transition.archive if transition else [],
            "new": transition.new if transition else [],
            "current_trace_ids": current_turn_trace_ids,
        })
    else:
        session_memory = None

    # clarify 降级逻辑：只有当前轮显式给出 trace 时才升级为排查，避免继承实体触发新排查
    if original_intent == "clarify" and has_explicit_trace_id and not state.memory_summary_mode:
        has_actionable_info = (
            state.facts.trace_id
            or state.facts.error_keywords
            or state.facts.username
            or state.facts.api_key_prefix
            or _is_exploratory_query(state.facts)
        )
        if has_actionable_info:
            logger.info(
                "clarify→troubleshoot downgrade: facts contain actionable info"
            )
            original_intent = "troubleshoot"
            state.facts.intent = "troubleshoot"

    # Multi-turn: merge accumulated facts and inject prior context
    if session and session_mgr:
        state.session_memory = session_memory
        if state.investigation_mode != "summary_only":
            state.facts = session_mgr.merge_facts(session, state.facts)
            if has_explicit_trace_id:
                state.facts.trace_ids = current_turn_trace_ids
                state.facts.trace_id = current_turn_trace_ids[0] if current_turn_trace_ids else state.facts.trace_id
                current_turn_trace_id = state.facts.trace_id
            elif not current_turn_trace_id and current_turn_has_time_range:
                state.facts.trace_id = None
        previously_context = _previously_investigated_context(session_memory, current_turn_trace_ids)
        if previously_context:
            state.prior_report_summary = (
                f"本次会话已排查记录：\n{previously_context}"
                if not state.prior_report_summary
                else f"{state.prior_report_summary}\n\n本次会话已排查记录：\n{previously_context}"
            )
        # 同 trace 追问可以注入前轮 evidence；总结/复用类追问只注入报告摘要。
        # 新 trace_id 代表独立问题，不能让前轮 evidence 参与本轮 Skill reroute。
        same_trace_followup = bool(
            state.investigation_mode == "new"
            and previous_turn_trace_ids
            and current_turn_trace_ids
            and set(previous_turn_trace_ids) == set(current_turn_trace_ids)
        )
        summary_memory_followup = state.investigation_mode in {"summary_only", "reuse_session"}
        should_inject_prior_evidence = same_trace_followup or (
            not current_turn_trace_id and not summary_memory_followup
        )
        if summary_memory_followup:
            recent_reports = _recent_assistant_reports(session)
            reuse_instruction = ""
            if state.investigation_mode == "reuse_session":
                reuse_instruction = "本轮命中历史 deep 深查记录，并且不是重查意图；请直接复用上述历史结论作答，明确说明未重新查询工具。\n\n"
            state.prior_report_summary = (
                f"{reuse_instruction}{recent_reports}"
                if not state.prior_report_summary
                else f"{state.prior_report_summary}\n\n{reuse_instruction}{recent_reports}"
            )
        elif should_inject_prior_evidence:
            if session.conversation_history:
                last_report = session.conversation_history[-1].get("content", "")
                if last_report:
                    state.prior_report_summary = (
                        last_report[:800]
                        if not state.prior_report_summary
                        else f"{state.prior_report_summary}\n\n前轮报告：\n{last_report[:800]}"
                    )
            for ae in session.accumulated_evidence:
                state.evidence.append(Evidence(
                    tool=ae["tool"],
                    args={},
                    result="",
                    summary=f"[前轮] {ae['summary']}",
                    trace_id=ae.get("trace_id"),
                ))

    # Summary/review turns should answer from short-term session memory only.
    if state.memory_summary_mode:
        emit("node_start", {"node": "reporter"})
        state = run_reporter(state)
        rag_history_index = getattr(state, "_rag_history_index", None)
        if rag_history_index:
            emit("rag_history_index", rag_history_index)
        emit("node_end", {"node": "reporter"})
        add_message(thread_id, "assistant", state.report, node="reporter")
        finalize_thread(thread_id, "done", 0, [])
        if session and session_mgr:
            session.thread_ids.append(thread_id)
            before_history_count = len(session.conversation_history)
            before_compressed_evidence_count = compressed_evidence_count()
            session_mgr.add_turn(
                session,
                user_query=user_query,
                report=state.report,
                facts=state.facts,
                evidence=[],
                skills=[],
            )
            emit_session_compression(before_history_count, before_compressed_evidence_count)
            record_session_memory_snapshot()
            save_session_snapshot(session, state)
        emit("done", state.report)
        try:
            from agent.debug import info_all
            info_all.write("final/state.json", state)
            info_all.write("final/report.json", {"report": state.report})
            info_all.end_run(debug_token)
        except Exception:
            pass
        return state.report

    # Short-circuit for non-troubleshoot intents
    if original_intent == "clarify":
        state.report = (
            "你的问题与网关/模型相关，但信息不足以发起排查。\n\n"
            "请补充以下信息以便定位问题：\n"
            "- **trace_id**：32 位请求追踪 ID\n"
            "- **时间范围**：问题发生的具体时间段\n"
            "- **错误信息**：报错码、异常提示等\n"
            "- **模型/用户名**：涉及的模型名称或账户\n\n"
            "示例：「2026-06-08 下午 trace_id abc123 报了 500 错误」"
        )
        try:
            from agent.debug import info_all
            info_all.write("final/state.json", state)
            info_all.write("final/report.json", {"report": state.report})
        except Exception:
            pass
        add_message(thread_id, "assistant", state.report, node="intake")
        finalize_thread(thread_id, "done", 0, [])
        if session and session_mgr:
            session.thread_ids.append(thread_id)
            before_history_count = len(session.conversation_history)
            before_compressed_evidence_count = compressed_evidence_count()
            session_mgr.add_turn(
                session,
                user_query=user_query,
                report=state.report,
                facts=state.facts,
                evidence=[],
                skills=[],
            )
            emit_session_compression(before_history_count, before_compressed_evidence_count)
            record_session_memory_snapshot()
            save_session_snapshot(session, state)
        emit("done", state.report)
        try:
            from agent.debug import info_all
            info_all.discard_current()
            info_all.end_run(debug_token)
        except Exception:
            pass
        return state.report

    if original_intent not in {"troubleshoot", "recheck"}:
        state.report = (
            "你好！我是 TokenHub 网关运维助手，基于 LangGraph 构建，专门帮助排查 API 网关相关问题。\n\n"
            "**我可以做的事情：**\n"
            "- 根据 trace_id 排查具体请求的失败原因\n"
            "- 扫描某段时间的整体异常情况\n"
            "- 分析超时、限流、认证失败等网关问题\n\n"
            "请描述你遇到的网关问题（如报错信息、trace_id、超时等），我会帮你分析。"
        )
        try:
            from agent.debug import info_all
            info_all.write("final/state.json", state)
            info_all.write("final/report.json", {"report": state.report})
        except Exception:
            pass
        add_message(thread_id, "assistant", state.report, node="intake")
        finalize_thread(thread_id, "done", 0, [])
        if session and session_mgr:
            session.thread_ids.append(thread_id)
            before_history_count = len(session.conversation_history)
            before_compressed_evidence_count = compressed_evidence_count()
            session_mgr.add_turn(
                session,
                user_query=user_query,
                report=state.report,
                facts=state.facts,
                evidence=[],
                skills=[],
            )
            emit_session_compression(before_history_count, before_compressed_evidence_count)
            record_session_memory_snapshot()
            save_session_snapshot(session, state)
        emit("done", state.report)
        try:
            from agent.debug import info_all
            info_all.discard_current()
            info_all.end_run(debug_token)
        except Exception:
            pass
        return state.report

    # --- skill_router ---
    emit("node_start", {"node": "skill_router"})
    state = run_skill_router(state) # 根据意图匹配技能

    # Skill 条件继承：仅当当前轮明确给出 trace_id，且与进入本轮前的上一轮 trace_id 相同时继承前轮 skills
    if not state.skills and session and session.skills_hit:
        if previous_turn_trace_ids and current_turn_trace_ids and set(previous_turn_trace_ids) == set(current_turn_trace_ids):
            state.skills = list(session.skills_hit[:2])
            emit("skill_inherit", {"skills": state.skills, "reason": "same_trace"})
        else:
            emit("skill_inherit_skip", {
                "reason": "trace_changed_or_missing",
                "previous_trace_id": previous_turn_trace_id,
                "previous_trace_ids": previous_turn_trace_ids,
                "current_trace_id": current_turn_trace_id,
                "current_trace_ids": current_turn_trace_ids,
            })

    emit("node_end", {"node": "skill_router", "skills": state.skills})

    # --- planner ⇄ executor → verifier loop ---
    async with mcp_session() as mcp:
        mcp_tools = await list_tools(mcp)
        mcp_tools = _tools_for_claude(mcp_tools)

        # Only expose code_explore to planner (not low-level grep/read)
        from agent.tools.code_reader import PLANNER_CODE_TOOLS
        all_tools = mcp_tools + PLANNER_CODE_TOOLS

        investigation_graph = _build_investigation_graph(
            mcp=mcp,
            mcp_tools=mcp_tools,
            all_tools=all_tools,
            emit=emit,
            on_event=on_event,
        )
        graph_result = await investigation_graph.ainvoke({"state": state, "verdict": "continue"})
        state = graph_result["state"]

    # --- reporter ---
    emit("node_start", {"node": "reporter"})
    state = run_reporter(state)
    rag_history_index = getattr(state, "_rag_history_index", None)
    if rag_history_index:
        emit("rag_history_index", rag_history_index)
    emit("node_end", {"node": "reporter"})

    # Persist final state
    add_message(thread_id, "assistant", state.report, node="reporter")
    finalize_thread(thread_id, "done", state.iterations, state.skills)

    # Multi-turn: update session with this turn's results
    if session and session_mgr:
        session.thread_ids.append(thread_id)
        before_history_count = len(session.conversation_history)
        before_compressed_evidence_count = compressed_evidence_count()
        session_mgr.add_turn(
            session,
            user_query=user_query,
            report=state.report,
            facts=state.facts,
            evidence=state.evidence,
            skills=state.skills,
        )
        update_current_investigation(ensure_session_memory(session), state)
        emit_session_compression(before_history_count, before_compressed_evidence_count)
        record_session_memory_snapshot()
        save_session_snapshot(session, state)

    emit("done", state.report)
    try:
        from agent.debug import info_all
        info_all.write("final/state.json", state)
        info_all.write("final/report.json", {"report": state.report})
        info_all.end_run(debug_token)
    except Exception:
        pass
    return state.report

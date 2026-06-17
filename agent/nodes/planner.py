"""Planner node: decide which MCP tools to call next."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml
import re
from agent.llm.registry import get_client
from agent.prompts import load
from agent.state import AgentState

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"


def _load_skill_meta(skill_id: str) -> dict:
    """Load frontmatter metadata for a matched skill."""
    for md in SKILLS_DIR.glob("S*_*.md"):
        if md.name.startswith(skill_id):
            text = md.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    return yaml.safe_load(parts[1]) or {}
                except Exception:
                    return {}
    return {}


def _load_skill_body(skill_id: str) -> str:
    """Load the full markdown body for a matched skill."""
    for md in SKILLS_DIR.glob("S*_*.md"):
        if md.name.startswith(skill_id):
            text = md.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            return parts[2].strip() if len(parts) >= 3 else text
    return ""


def _tools_hint_diff(skill_ids: list[str], called_tools: set[str]) -> str:
    """Build a summary of required/optional tools not yet called."""
    lines = []
    for sid in skill_ids:
        meta = _load_skill_meta(sid)
        tools_hint = meta.get("tools_hint", {})
        if isinstance(tools_hint, list):
            required = tools_hint
            optional = []
        elif isinstance(tools_hint, dict):
            required = tools_hint.get("required", [])
            optional = tools_hint.get("optional", [])
        else:
            continue
        remaining_required = [t for t in required if t not in called_tools]
        remaining_optional = [t for t in optional if t not in called_tools]
        if remaining_required or remaining_optional:
            parts = [f"[{sid}]"]
            if remaining_required:
                parts.append(f"  必须工具（尚未调用）: {', '.join(remaining_required)}")
            if remaining_optional:
                parts.append(f"  可选工具（尚未调用）: {', '.join(remaining_optional)}")
            lines.append("\n".join(parts))
    return "\n".join(lines)


def _evidence_summary(state: AgentState) -> str:
    """Build a summary of evidence collected so far."""
    if not state.evidence:
        return "（暂无证据）"
    parts = []
    for i, e in enumerate(state.evidence, 1):
        if e.tool == "_verifier_hint":
            continue
        flags = []
        if e.truncated:
            flags.append("[内容裁短]")
        if e.hit_log_limit:
            flags.append("[触及上限，可继续查询]")
        flag_str = " ".join(flags)
        parts.append(
            f"{i}. **{e.tool}** {flag_str}\n"
            f"   参数: {json.dumps(e.args, ensure_ascii=False)}\n"
            f"   返回大小: {e.result_size} 字节\n"
            f"   摘要: {e.summary or e.result[:300]}"
        )
    return "\n".join(parts)


def _relevant_code_context(state: AgentState) -> tuple[str, int]:
    """P3.6: Inject gateway source architecture index for planner.

    On iteration 0: inject the full architecture index (~500 tokens)
    so planner knows which code tools and source files are available.

    On iteration >= 1: no additional injection — planner should use
    code_grep/code_read_file tools to read source code directly.

    Returns (context_text, hit_count).
    """
    if state.memory_summary_mode or state.iterations > 0:
        return "", 0

    try:
        from agent.config import GATEWAY_SOURCE_DIR
        from pathlib import Path

        source_dir = Path(GATEWAY_SOURCE_DIR)
        if not source_dir.exists():
            return "", 0

        index_path = source_dir / "architecture-index.md"
        if not index_path.exists():
            return "", 0

        content = index_path.read_text(encoding="utf-8")
        return content, 1
    except Exception:
        return "", 0


def _session_memory_context(state: AgentState) -> str:
    """Build L1 session memory context for planner."""
    memory = getattr(state, "session_memory", None)
    if not memory:
        return ""

    parts: list[str] = []
    if getattr(memory, "current_state", ""):
        parts.append(f"当前会话状态：{memory.current_state}")

    segments_summary = memory.get_segments_summary(max_segments=5)
    if segments_summary:
        parts.append("本次会话已完成的排查：\n" + segments_summary)

    if getattr(memory, "discovered_issues", None):
        issues = "\n".join(f"- {item}" for item in memory.discovered_issues[-5:])
        parts.append("本次会话已发现问题：\n" + issues)

    if getattr(memory, "cross_trace_findings", ""):
        parts.append(f"跨 trace 关联发现：{memory.cross_trace_findings}")

    return "\n\n".join(parts)



def _extract_json_array_with_status(text: str) -> tuple[list, bool]:
    """Extract the last valid JSON array from text and whether an array was found."""
    last_bracket = text.rfind("]")
    while last_bracket >= 0:
        start = text.rfind("[", 0, last_bracket)
        while start >= 0:
            try:
                candidate = text[start : last_bracket + 1]
                parsed = json.loads(candidate)
                if isinstance(parsed, list):
                    return parsed, True
            except (json.JSONDecodeError, ValueError):
                pass
            start = text.rfind("[", 0, start)
        last_bracket = text.rfind("]", 0, last_bracket)
    return [], False


def _extract_json_array(text: str) -> list:
    """Extract the last valid JSON array from text (reverse search).

    Searches from the end of the text to avoid matching brackets inside
    <GLOBAL_PLAN> or other non-JSON content that appears before the plan.
    """
    parsed, _ = _extract_json_array_with_status(text)
    return parsed


def _tools_description(tools: list[dict]) -> str:
    """Format MCP tools for prompt."""
    lines = []
    for t in tools:
        lines.append(f"- **{t['name']}**: {t['description']}")
    return "\n".join(lines)


def run_planner(state: AgentState, mcp_tools: list[dict]) -> AgentState:
    """Use LLM to plan next tool calls."""
    if state.memory_summary_mode:
        state.plan = []
        try:
            from agent.debug import info_all
            info_all.write(f"planner/iteration-{state.iterations + 1:02d}-context.json", {
                "skipped": True,
                "reason": "memory_summary_mode",
            })
            info_all.write(f"planner/iteration-{state.iterations + 1:02d}-plan.json", {
                "iteration": state.iterations + 1,
                "plan": [],
                "reason": "memory_summary_mode",
            })
        except Exception:
            pass
        return state

    llm = get_client("planner")

    # Build valid tool name set for validation
    valid_tool_names = {t["name"] for t in mcp_tools}

    # Build skill guidance from matched skills
    skill_guidance = ""
    for sid in state.skills:
        body = _load_skill_body(sid)
        if body:
            skill_guidance += f"\n### {sid} 排查指引\n{body}\n"
    if not skill_guidance:
        skill_guidance = "无特定 Skill 匹配，使用通用排查策略。"

    prompt = load(
        "planner",
        tools_description=_tools_description(mcp_tools),
        skill_guidance=skill_guidance,
    )

    # Build context message
    context_parts = [
        f"用户问题：{state.user_query}",
        f"抽取结果：trace_id={state.facts.trace_id}, trace_ids={state.facts.trace_ids}, "
        f"model={state.facts.model}, provider={state.facts.provider}, "
        f"time={state.facts.time_start}~{state.facts.time_end}",
        f"命中 Skill：{state.skills or '无'}",
        f"当前迭代：{state.iterations + 1} / {state.MAX_PLAN_VERIFY_LOOPS}",
        f"本问题已调用工具数：{state.query_tool_calls} / {state.MAX_QUERY_TOOL_CALLS}",
        f"当前 state evidence 数：{state.total_tool_calls} / {state.MAX_TOTAL_TOOL_CALLS}",
    ]

    # Inject Skill tools_hint diff: show required tools not yet called
    if state.skills:
        called_tools = {e.tool for e in state.evidence if e.tool != "_verifier_hint"}
        hint_diff = _tools_hint_diff(state.skills, called_tools)
        if hint_diff:
            context_parts.append(f"\nSkill 工具使用进度：\n{hint_diff}")

    # Inject global plan for subsequent iterations
    if state.global_plan:
        context_parts.append(f"\n全局排查计划：\n{state.global_plan}")

    # Inject previous report summary for same-trace follow-up questions.
    if state.prior_report_summary:
        context_parts.append(f"\n前轮排查结论：\n{state.prior_report_summary}")

    # Inject L1 SessionMemory for deterministic same-session context.
    session_memory_context = _session_memory_context(state)
    if session_memory_context:
        context_parts.append(f"\n本次会话结构化记忆：\n{session_memory_context}")

    # Include verifier hint if available (from previous iteration)
    evidence_text = _evidence_summary(state)
    if state.evidence:
        context_parts.append(f"\n已有证据：\n{evidence_text}")

    # Include accumulator status
    if state.accumulator:
        context_parts.append(f"\n累加器状态：{list(state.accumulator.keys())}")

    # P3.6: Inject architecture index on first iteration (code tools available for planner)
    code_ctx, code_hits = _relevant_code_context(state)
    if code_ctx:
        context_parts.append(f"\n{code_ctx}")

    state._rag_hits = {"history": 0}

    try:
        from agent.debug import info_all
        info_all.write(f"planner/iteration-{state.iterations + 1:02d}-context.json", {
            "context_parts": context_parts,
            "rag_hits": state._rag_hits,
            "skill_guidance": "[omitted: skill details]" if state.skills else skill_guidance,
            "tools": [t.get("name") for t in mcp_tools],
        })
    except Exception:
        pass

    max_plan_attempts = 3
    response = None
    plan = []
    parse_errors: list[dict] = []
    explicit_empty_plan = False
    plan_attempts_made = 0

    for attempt in range(1, max_plan_attempts + 1):
        plan_attempts_made = attempt
        attempt_context = list(context_parts)
        if attempt > 1:
            previous_error = parse_errors[-1]["reason"] if parse_errors else "planner_output_invalid"
            attempt_context.append(
                "\n上一次 planner 输出格式无效，必须修正后重试。"
                f"失败原因：{previous_error}。"
                "只允许输出 <GLOBAL_PLAN>...</GLOBAL_PLAN> 和 JSON 数组；"
                "JSON 数组中每项必须是 {\"tool\": \"工具名\", \"args\": {...}}，"
                "工具名必须来自可用 MCP 工具列表。"
            )

        response = llm.invoke(
            messages=[{"role": "user", "content": "\n".join(attempt_context)}],
            system=prompt,
        )

        # Extract global plan on first iteration
        if state.iterations == 0 and not state.global_plan:
            gp_match = re.search(
                r"<GLOBAL_PLAN>(.*?)</GLOBAL_PLAN>", response.text, re.DOTALL
            )
            if gp_match:
                state.global_plan = gp_match.group(1).strip()
                logger.info("Global plan extracted:\n%s", state.global_plan)

        parsed, found_json_array = _extract_json_array_with_status(response.text)
        if not found_json_array:
            parse_errors.append({
                "attempt": attempt,
                "reason": "no_valid_json_array",
                "response_length": len(response.text),
            })
            logger.warning(
                "Planner output contains no valid JSON array (attempt=%d/%d, response length=%d)",
                attempt,
                max_plan_attempts,
                len(response.text),
            )
            continue

        if not parsed:
            explicit_empty_plan = True
            break

        invalid_items = 0
        for item in parsed:
            if not isinstance(item, dict) or "tool" not in item:
                invalid_items += 1
                continue
            tool_name = item["tool"]
            # Validate tool name against MCP tool list
            if tool_name not in valid_tool_names:
                invalid_items += 1
                logger.warning("Planner generated invalid tool '%s', skipping", tool_name)
                continue
            plan.append({"tool": tool_name, "args": item.get("args", {})})

        if plan:
            break

        parse_errors.append({
            "attempt": attempt,
            "reason": "json_array_without_valid_tool_calls",
            "item_count": len(parsed),
            "invalid_items": invalid_items,
        })
        logger.warning(
            "Planner JSON array contains no valid tool calls (attempt=%d/%d, items=%d, invalid=%d)",
            attempt,
            max_plan_attempts,
            len(parsed),
            invalid_items,
        )

    # Fallback: if LLM chose no tools without format errors, use heuristic on first iteration.
    # If planner output format is invalid after retries, keep current behavior: empty plan -> reporter.
    if not plan and state.iterations == 0 and not parse_errors and not explicit_empty_plan:
        trace_ids = list(dict.fromkeys(state.facts.trace_ids or ([state.facts.trace_id] if state.facts.trace_id else [])))
        if len(trace_ids) > 1:
            overview_trace_ids = trace_ids[:5]
            plan = [{"tool": "sls_query_gateway_usage_overview", "args": {
                "filters": [{"field": "trace_id", "op": "eq", "value": overview_trace_ids}],
                "start_time": state.facts.time_start or "",
                "end_time": state.facts.time_end or "",
                "limit": len(overview_trace_ids),
            }}]
        elif state.facts.trace_id:
            # Overview-first: 先用 overview 定位精确时间，再做详细查询
            plan = [{"tool": "sls_query_gateway_usage_overview", "args": {
                "filters": [{"field": "trace_id", "op": "eq", "value": state.facts.trace_id}],
                "start_time": state.facts.time_start or "",
                "end_time": state.facts.time_end or "",
                "limit": 1,
            }}]
        elif state.facts.username:
            plan = [{"tool": "db_get_user_api_key_prefixes", "args": {
                "username": state.facts.username,
            }}]
        else:
            plan = [{"tool": "sls_search_errors", "args": {
                "start_time": state.facts.time_start or "",
                "end_time": state.facts.time_end or "",
                "limit": 1000,
            }}]

    state.plan = plan
    try:
        from agent.debug import info_all
        info_all.write(f"planner/iteration-{state.iterations + 1:02d}-plan.json", {
            "iteration": state.iterations + 1,
            "global_plan": state.global_plan,
            "plan": state.plan,
            "planner_attempts": plan_attempts_made,
            "parse_errors": parse_errors,
            "explicit_empty_plan": explicit_empty_plan,
            "raw_response": response.text if response else "",
        })
    except Exception:
        pass
    return state

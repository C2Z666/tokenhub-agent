"""Verifier node: check if evidence is sufficient to produce a report."""
from __future__ import annotations

import json
import re

from agent.llm.registry import get_client
from agent.prompts import load
from agent.state import AgentState


def _evidence_summary(state: AgentState) -> str:
    """Build evidence summary for verifier prompt."""
    if not state.evidence:
        return "（暂无证据）"
    parts = []
    for i, e in enumerate(state.evidence, 1):
        if e.tool == "_verifier_hint":
            continue
        flags = []
        if e.truncated:
            flags.append("[内容裁短，不可恢复]")
        if e.hit_log_limit:
            flags.append("[触及上限，可继续查询]")
        flag_str = " ".join(flags)
        # Show first 500 chars of result
        preview = e.summary or e.result[:500]
        if not e.summary and len(e.result) > 500:
            preview += "..."
        parts.append(
            f"{i}. **{e.tool}** {flag_str}\n"
            f"   参数: {json.dumps(e.args, ensure_ascii=False)}\n"
            f"   结果预览: {preview}"
        )
    return "\n".join(parts)


def run_verifier(state: AgentState) -> tuple[AgentState, str]:
    """Return (state, verdict) where verdict is 'done' or 'continue'."""
    # Force done if we've exhausted iterations or tool calls
    if not state.can_iterate or not state.can_call_more_tools:
        return state, "done"

    llm = get_client("verifier")
    prompt = load(
        "verifier",
        evidence_summary=_evidence_summary(state),
        iteration=str(state.iterations + 1),
        max_iterations=str(state.MAX_PLAN_VERIFY_LOOPS),
    )

    response = llm.invoke(
        messages=[{"role": "user", "content": f"用户问题：{state.user_query}\n命中 Skill：{state.skills}"}],
        system=prompt,
    )

    # Parse verdict. If the model self-corrects and emits multiple JSON blocks,
    # trust the last explicit verdict.
    verdict = "done"
    matches = re.findall(r'"verdict"\s*:\s*"(done|continue)"', response.text)
    if matches:
        verdict = matches[-1]
    elif "continue" in response.text.lower() and state.can_iterate:
        verdict = "continue"

    # Extract hint for planner if continuing
    if verdict == "continue":
        hint_m = re.search(r'"hint"\s*:\s*"([^"]+)"', response.text)
        if hint_m:
            # Store hint as a pseudo-evidence for planner to see
            from agent.state import Evidence
            state.evidence.append(Evidence(
                tool="_verifier_hint",
                args={},
                result=hint_m.group(1),
                summary=f"Verifier 建议: {hint_m.group(1)}",
            ))

    return state, verdict

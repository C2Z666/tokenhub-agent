"""Reporter node: generate the final structured report."""
from __future__ import annotations

import json
import logging
import re

from agent.llm.registry import get_client
from agent.persistence import save_report
from agent.prompts import load
from agent.state import AgentState

logger = logging.getLogger(__name__)


def _build_evidence_text(state: AgentState) -> str:
    """Build concise evidence text for user message (not system prompt)."""
    later_success_tools: set[str] = set()
    resolved_error_indexes: set[int] = set()
    for i in range(len(state.evidence) - 1, -1, -1):
        e = state.evidence[i]
        if e.tool == "_verifier_hint":
            continue
        if e.summary.startswith("ERROR:") and e.tool in later_success_tools:
            resolved_error_indexes.add(i)
        elif not e.summary.startswith("ERROR:"):
            later_success_tools.add(e.tool)

    parts = []
    for i, e in enumerate(state.evidence, 1):
        if e.tool == "_verifier_hint":
            continue
        if (i - 1) in resolved_error_indexes:
            continue
        flags = []
        if e.truncated:
            flags.append("[内容裁短]")
        if e.hit_log_limit:
            flags.append("[触及上限]")
        flag_str = " ".join(flags)
        preview = e.summary or e.result[:500]

        parts.append(
            f"### 证据 {i}: {e.tool} {flag_str}\n"
            f"参数: `{json.dumps(e.args, ensure_ascii=False)}`\n"
            f"摘要: {preview}"
        )
    if state.accumulator:
        parts.append("\n### 累加数据\n")
        for key, pages in state.accumulator.items():
            parts.append(f"**{key}**: {len(pages)} 页数据已收集")
    return "\n\n".join(parts)


def _extract_conclusion(report: str) -> str | None:
    """Extract the conclusion section from report."""
    m = re.search(r"##\s*结论\s*\n(.+?)(?=\n##|\Z)", report, re.DOTALL)
    return m.group(1).strip() if m else None


def _extract_recommendations(report: str) -> str | None:
    """Extract repair recommendations from report."""
    m = re.search(r"##\s*修复建议\s*\n(.+?)(?=\n##|\Z)", report, re.DOTALL)
    return m.group(1).strip() if m else None



def _extract_root_cause(report: str) -> str | None:
    """Extract root cause text from common report sections."""
    for heading in ("根因", "原因分析", "问题原因"):
        m = re.search(rf"##\s*{heading}\s*\n(.+?)(?=\n##|\Z)", report, re.DOTALL)
        if m:
            return m.group(1).strip()
    return None



def _dedup_paragraphs(text: str) -> str:
    """Detect and remove duplicate paragraphs (split by blank lines).

    If a paragraph appears more than once, only keep the first occurrence.
    """
    paragraphs = text.split("\n\n")
    seen: set[str] = set()
    deduped: list[str] = []
    removed = 0
    for p in paragraphs:
        normalized = p.strip()
        if not normalized:
            deduped.append(p)
            continue
        if normalized in seen:
            removed += 1
            continue
        seen.add(normalized)
        deduped.append(p)
    if removed > 0:
        logger.warning("Reporter dedup: removed %d duplicate paragraph(s)", removed)
    return "\n\n".join(deduped)


def _expand_short_trace_ids(report: str, state: AgentState) -> str:
    """Expand 8-char trace_id mentions when evidence has a unique full trace_id."""
    source = "\n".join(
        [
            state.user_query,
            state.prior_report_summary or "",
            *[
                "\n".join([
                    json.dumps(e.args, ensure_ascii=False),
                    e.summary or "",
                    e.result or "",
                ])
                for e in state.evidence
            ],
        ]
    )
    full_trace_ids = set(re.findall(r"\b[0-9a-fA-F]{32}\b", source))
    if not full_trace_ids:
        return report

    by_prefix: dict[str, str] = {}
    ambiguous: set[str] = set()
    for trace_id in full_trace_ids:
        prefix = trace_id[:8]
        if prefix in by_prefix and by_prefix[prefix] != trace_id:
            ambiguous.add(prefix)
        else:
            by_prefix[prefix] = trace_id

    for prefix in ambiguous:
        by_prefix.pop(prefix, None)

    def replace(match: re.Match) -> str:
        value = match.group(0)
        return by_prefix.get(value, value)

    return re.sub(r"\b[0-9a-fA-F]{8}\b", replace, report)


def run_reporter(state: AgentState) -> AgentState:
    """Generate a structured report and persist it."""
    llm = get_client("reporter")
    evidence_text = _build_evidence_text(state)

    skills_str = ", ".join(state.skills) if state.skills else "无"
    prompt = load("reporter", skills=skills_str)

    context = (
        f"用户问题：{state.user_query}\n"
        f"命中 Skill：{skills_str}\n"
        f"抽取实体：trace_id={state.facts.trace_id}, trace_ids={state.facts.trace_ids}, "
        f"model={state.facts.model}, provider={state.facts.provider}\n"
        f"迭代次数：{state.iterations}\n"
        f"本问题工具调用次数：{state.query_tool_calls}\n"
        f"当前 state evidence 数：{state.total_tool_calls}\n"
    )
    trace_ids = list(dict.fromkeys(state.facts.trace_ids or ([] if not state.facts.trace_id else [state.facts.trace_id])))
    if len(trace_ids) > 1:
        context += (
            "\n## 多 trace 输出约束\n\n"
            f"本轮包含 {len(trace_ids)} 个 trace_id，必须按问题类别聚合输出。"
            "不要逐个 trace_id 展开模板化字段清单；"
            "最多只列每类 3 个代表 trace_id。"
            "只有真正深查过、异常类型不同、或作为代表样本的 trace 才允许展开细节。\n"
        )
    if state.prior_report_summary:
        context += f"\n## 短时会话记忆\n\n{state.prior_report_summary}\n"
    context += f"\n## 证据详情\n\n{evidence_text}"

    response = llm.invoke(
        messages=[{"role": "user", "content": context}],
        system=prompt,
    )

    state.report = _expand_short_trace_ids(_dedup_paragraphs(response.text), state)

    # Persist report
    conclusion = _extract_conclusion(state.report)
    recommendations = _extract_recommendations(state.report)
    root_cause = _extract_root_cause(state.report)
    evidence_records = [
        {
            "tool": e.tool,
            "args": e.args,
            "summary": e.summary or e.result[:200],
            "trace_id": e.trace_id,
        }
        for e in state.evidence if e.tool != "_verifier_hint"
    ]

    save_report(
        thread_id=state.thread_id,
        full_report=state.report,
        conclusion=conclusion,
        evidence=evidence_records,
        recommendations=recommendations,
        skills_hit=state.skills,
    )

    state._rag_history_index = {
        "status": "skipped",
        "reason": "history_rag_disabled",
    }
    logger.info("Skip history indexing: history RAG disabled")

    return state

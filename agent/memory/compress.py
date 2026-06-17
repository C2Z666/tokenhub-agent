"""Compression strategies for session memory."""
from __future__ import annotations

import logging
import re
from typing import Any

from agent.config import SESSION_COMPRESS_AFTER, SESSION_HISTORY_COMPRESS_AFTER

logger = logging.getLogger(__name__)

MAX_EVIDENCE_PER_TURN = 5
MAX_EVIDENCE_SUMMARY_CHARS = 150
MAX_HISTORY_MESSAGE_CHARS = 300


def compress_evidence_by_turn(
    accumulated_evidence: list[dict[str, Any]],
    cutoff_turn: int,
) -> list[dict[str, Any]]:
    """按轮压缩旧 evidence，保留 tool、summary 和 trace_id。"""
    old = [e for e in accumulated_evidence if e.get("turn", 0) <= cutoff_turn]
    recent = [e for e in accumulated_evidence if e.get("turn", 0) > cutoff_turn]
    if not old:
        return accumulated_evidence

    turn_groups: dict[int, list[dict[str, Any]]] = {}
    for entry in old:
        turn = int(entry.get("turn") or 0)
        turn_groups.setdefault(turn, []).append(entry)

    compressed_entries: list[dict[str, Any]] = []
    for turn_idx in sorted(turn_groups):
        key_findings: list[str] = []
        trace_ids: list[str] = []
        for entry in turn_groups[turn_idx]:
            entry_trace_ids = entry.get("trace_ids") or []
            if not entry_trace_ids and entry.get("trace_id"):
                entry_trace_ids = [entry.get("trace_id")]
            for trace_id in entry_trace_ids:
                if trace_id and trace_id not in trace_ids:
                    trace_ids.append(trace_id)

            if entry.get("tool") == "_compressed":
                summary = str(entry.get("summary", ""))
                if summary:
                    key_findings.append(summary)
                continue

            tool = str(entry.get("tool") or "unknown")
            summary = str(entry.get("summary") or "")[:MAX_EVIDENCE_SUMMARY_CHARS]
            if summary:
                key_findings.append(f"{tool}: {summary}")
            else:
                key_findings.append(tool)

        compressed_entries.append({
            "turn": turn_idx,
            "tool": "_compressed",
            "summary": f"[第{turn_idx}轮] " + "; ".join(key_findings[:MAX_EVIDENCE_PER_TURN]),
            "trace_id": trace_ids[0] if trace_ids else None,
            "trace_ids": trace_ids,
        })

    return compressed_entries + recent


def compress_history_with_llm(
    conversation_history: list[dict[str, str]],
) -> list[dict[str, str]]:
    """用 LLM 将旧对话压缩为结构化摘要，失败时回退到规则压缩。"""
    if len(conversation_history) < SESSION_HISTORY_COMPRESS_AFTER:
        return conversation_history

    recent_count = SESSION_COMPRESS_AFTER * 2
    if recent_count <= 0 or recent_count >= len(conversation_history):
        return conversation_history

    old = conversation_history[:-recent_count]
    recent = conversation_history[-recent_count:]

    old_text = "\n".join(
        f"[{msg.get('role', '')}] {msg.get('content', '')[:MAX_HISTORY_MESSAGE_CHARS]}"
        for msg in old
    )
    prompt = f"""请将以下多轮对话压缩为结构化摘要，保留关键信息：

{old_text}

要求输出格式：
1. 排查过的 trace_id 及其结论（每个一行）
2. 发现的关键问题（每个一行）
3. 使用的排查方法和关键证据（简要列举）
不要保留原始对话格式，只保留事实性结论。"""

    try:
        from agent.llm.registry import get_client

        client = get_client("intake")
        response = client.invoke(messages=[{"role": "user", "content": prompt}])
        summary = response.text.strip()
        if not summary:
            raise ValueError("empty LLM compression result")
        logger.info("Compressed conversation history with LLM, old entries=%d", len(old))
    except Exception as exc:
        logger.warning("LLM history compression failed, fallback to rule compression: %s", exc)
        summary = _compress_history_by_rules(old)

    compressed_entry = {
        "role": "system",
        "content": f"[历史摘要 - 已压缩 {len(old)} 条消息]\n{summary}",
    }
    return [compressed_entry] + recent


def _compress_history_by_rules(middle: list[dict[str, str]]) -> str:
    key_facts: list[str] = []
    for msg in middle:
        content = msg.get("content", "")
        role = msg.get("role", "")
        if role == "user":
            key_facts.append(f"用户问: {content[:100]}")
            continue

        if role == "assistant":
            conclusion_match = re.search(
                r"##\s*结论\s*\n(.*?)(?:\n##|\Z)",
                content,
                re.DOTALL,
            )
            if conclusion_match:
                conclusion = conclusion_match.group(1).strip()[:200]
                key_facts.append(f"结论: {conclusion}")
            else:
                key_facts.append(f"回答摘要: {content[:150]}")
            continue

        if content:
            key_facts.append(f"{role}: {content[:120]}")

    return "\n".join(key_facts) or "无可提取的历史事实。"

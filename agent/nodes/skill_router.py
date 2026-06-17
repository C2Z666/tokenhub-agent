"""Skill Router node: match user query to candidate Skills."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml

from agent.llm.registry import get_client
from agent.prompts import load
from agent.state import AgentState

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"
OVERVIEW_TOOL = "sls_query_gateway_usage_overview" # 这个工具跳过RAG匹配kany


def _load_skill_metadata() -> list[dict]:
    """Load frontmatter from all skills/*.md files."""
    skills = []
    for md in sorted(SKILLS_DIR.glob("S*_*.md")):
        text = md.read_text(encoding="utf-8")
        # Extract YAML frontmatter between ---
        m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if m:
            meta = yaml.safe_load(m.group(1))
            meta["_file"] = md.name
            skills.append(meta)
    return skills


def _skills_summary(skills_meta: list[dict]) -> str:
    """Generate a brief summary of available skills for prompt injection."""
    lines = []
    for s in skills_meta:
        name = s.get("name", "unknown")
        desc = s.get("description", "")
        kw = ", ".join(str(k) for k in s.get("keywords", [])[:8])
        lines.append(f"- **{name}**: {desc}\n  关键词: {kw}")
    return "\n".join(lines)


def _build_strong_signal_map(skills_meta: list[dict]) -> dict[str, str]:
    """Build signal → skill_id mapping from all Skills' frontmatter strong_signals.

    Each signal should belong to exactly one Skill (mutually exclusive).
    Logs a warning on conflicts and uses first-come-first-served.
    """
    signal_map: dict[str, str] = {}
    conflicts: list[str] = []
    for s in skills_meta:
        name = s.get("name", "")
        skill_id = name.split("-")[0] if "-" in name else ""
        if not skill_id:
            continue
        for signal in s.get("strong_signals", []):
            key = str(signal).lower()
            if key in signal_map:
                conflicts.append(
                    f"signal '{signal}' in both {signal_map[key]} and {skill_id}"
                )
            else:
                signal_map[key] = skill_id
    if conflicts:
        logger.warning("strong_signals conflicts (first-come-first-served): %s",
                        "; ".join(conflicts))
    return signal_map


def _strong_signal_match(signal: str, text_lower: str) -> tuple[int, int] | None:
    """Match strong signals safely.

    Bare HTTP status codes must appear in status-code context; otherwise short
    numeric signals like "403" can match request IDs, trace IDs, cache keys, or API key IDs.
    """
    signal = signal.strip().lower()
    if not signal:
        return None

    if re.fullmatch(r"\d{3}", signal):
        for context in re.finditer(
            r"(?:http状态码|status\s+code|status|http)[=:\s]*([^\n;。]{0,80})",
            text_lower,
        ):
            for code in re.finditer(r"(?<!\d)\d{3}(?!\d)", context.group(1)):
                if code.group(0) == signal:
                    start = context.start(1) + code.start()
                    return start, start + len(signal)
        return None

    match = re.search(re.escape(signal), text_lower)
    if not match:
        return None
    return match.start(), match.end()


def _match_context(text: str, start: int, end: int, window: int = 80) -> str:
    left = max(0, start - window)
    right = min(len(text), end + window)
    return text[left:right].replace("\n", " ")



def _keyword_hit(keyword: str, text: str) -> bool:
    """Match keywords without treating digits inside trace IDs as status codes."""
    kw = keyword.lower()
    if re.fullmatch(r"\d{3}", kw):
        return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", text) is not None
    return kw in text


def _keyword_match(facts, skills_meta: list[dict]) -> list[str]:
    """Rule-based keyword matching as fallback."""
    query_lower = facts.raw_query.lower()
    all_keywords = " ".join(facts.error_keywords).lower()
    text = f"{query_lower} {all_keywords}"

    scores: dict[str, int] = {}
    for s in skills_meta:
        name = s.get("name", "")
        skill_id = name.split("-")[0] if "-" in name else ""
        kws = s.get("keywords", [])
        score = sum(1 for kw in kws if _keyword_hit(str(kw), text))
        if score > 0:
            scores[skill_id] = score

    # Return top-2 by score
    sorted_skills = sorted(scores.items(), key=lambda x: -x[1])
    return [sid for sid, _ in sorted_skills[:2]]


def run_skill_router(state: AgentState) -> AgentState:
    """Route user query to candidate Skills."""
    skills_meta = _load_skill_metadata()
    summary = _skills_summary(skills_meta)

    # Try LLM-based routing first
    llm = get_client("skill_router")
    prompt = load("skill_router", skills_summary=summary)

    facts_str = json.dumps({
        "trace_id": state.facts.trace_id,
        "model": state.facts.model,
        "provider": state.facts.provider,
        "error_keywords": state.facts.error_keywords,
        "raw_query": state.facts.raw_query,
    }, ensure_ascii=False)

    response = llm.invoke(
        messages=[{"role": "user", "content": f"提取结果：\n{facts_str}"}],
        system=prompt,
    )

    # Parse LLM response
    matched = []
    m = re.search(r"\[.*?\]", response.text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            matched = [s for s in parsed if isinstance(s, str) and s.startswith("S")]
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback to keyword matching if LLM returns nothing
    if not matched:
        matched = _keyword_match(state.facts, skills_meta)

    state.skills = matched[:2]  # top-k = 2
    return state

def reroute_from_embedding(state: AgentState) -> AgentState:
    """Level-2 reroute: semantic matching via RAG embedding.

    Uses evidence summaries (or user query as fallback) to find the most
    similar Skill documents in the vector store.  Only fires when Level 0
    and Level 1 both failed to produce skills.
    """
    if state.skills:
        return state

    from agent.config import RAG_SKILL_THRESHOLD
    from agent.rag import get_embedder, get_store

    # Build query text from non-overview evidence summaries.
    # Overview data is too coarse; it can trigger strong-signal routing but
    # should not drive semantic Skill RAG matching.
    non_overview_evidence = [
        e for e in state.evidence
        if e.tool not in ("_verifier_hint", OVERVIEW_TOOL) and e.summary
    ]
    evidence_text = "\n".join(e.summary for e in non_overview_evidence)

    # Fallback to user query only before any evidence exists. If we only have
    # overview evidence, skip Skill RAG and wait for trace/request details.
    query_text = evidence_text or (state.user_query if not state.evidence else "")
    if not query_text:
        if state.evidence:
            state._skill_rag_debug = {
                "type": "skill",
                "count": 0,
                "threshold": RAG_SKILL_THRESHOLD,
                "top_score": None,
                "matched_skills": [],
                "reason": "skip_overview_only",
            }
            try:
                from agent.debug import info_all
                info_all.write("rag/skill_reroute.json", {
                    "skipped": True,
                    "reason": "skip_overview_only",
                    "overview_tools": [e.tool for e in state.evidence if e.tool == OVERVIEW_TOOL],
                    "threshold": RAG_SKILL_THRESHOLD,
                    "matched_skills": [],
                    "results": [],
                })
            except Exception:
                pass
        return state

    state._rag_entries = getattr(state, "_rag_entries", []) + [{
        "type": "skill",
        "reason": "level_2_skill_reroute",
    }]

    store = get_store()
    embedder = get_embedder()

    query_embedding = embedder.embed_text(query_text)
    top_k = 8
    results = store.search_similar(
        query_embedding=query_embedding,
        top_k=top_k,
        filters={"source_type": "skill"},
    )

    best_by_skill = {}
    for r in results:
        sid = r.metadata.get("skill_id", r.source_id)
        if sid not in best_by_skill or r.score > best_by_skill[sid].score:
            best_by_skill[sid] = r
    ranked_skill_hits = sorted(best_by_skill.items(), key=lambda item: -item[1].score)

    matched = [sid for sid, r in ranked_skill_hits if r.score >= RAG_SKILL_THRESHOLD]
    confidence_gap_applied = False
    if len(matched) >= 2:
        first_score = ranked_skill_hits[0][1].score
        second_score = ranked_skill_hits[1][1].score
        if first_score - second_score > 0.15:
            matched = matched[:1]
            confidence_gap_applied = True
    if matched:
        state.skills = matched[:2]

    state._skill_rag_debug = {
        "type": "skill",
        "count": len(matched),
        "threshold": RAG_SKILL_THRESHOLD,
        "top_score": ranked_skill_hits[0][1].score if ranked_skill_hits else None,
        "matched_skills": state.skills,
        "confidence_gap_applied": confidence_gap_applied,
    }

    try:
        from agent.debug import info_all
        info_all.write("rag/skill_reroute.json", {
            "query_text": query_text,
            "top_k": top_k,
            "threshold": RAG_SKILL_THRESHOLD,
            "matched_skills": state.skills,
            "confidence_gap_applied": confidence_gap_applied,
            "ranked_skill_hits": [
                {
                    "skill_id": sid,
                    "score": r.score,
                    "matched": r.score >= RAG_SKILL_THRESHOLD,
                    "title": r.title,
                    "metadata": r.metadata,
                    "content_preview": r.content[:800],
                }
                for sid, r in ranked_skill_hits
            ],
            "results": [
                {
                    "score": r.score,
                    "matched": r.score >= RAG_SKILL_THRESHOLD,
                    "source_id": r.source_id,
                    "skill_id": r.metadata.get("skill_id", r.source_id),
                    "metadata": r.metadata,
                    "content_preview": r.content[:800],
                }
                for r in results
            ],
        })
    except Exception:
        pass

    if state.skills:
        logger.info("Level-2 skill RAG matched: %s (threshold=%.2f, scores=%s)",
                    state.skills,
                    RAG_SKILL_THRESHOLD,
                    [f"{r.score:.4f}" for r in results])
    else:
        logger.info("Level-2 skill RAG no match (threshold=%.2f, scores=%s)",
                    RAG_SKILL_THRESHOLD,
                    [f"{r.score:.4f}" for r in results])

    return state


# 根据证据匹配skill
def reroute_from_evidence(state: AgentState) -> AgentState:
    """Level-1 reroute: match skills from evidence when initial routing found nothing.

    Two-phase matching:
    1. Strong signal matching — single hit is enough (from frontmatter strong_signals)
    2. Keyword matching — requires score >= 2 (existing logic)
    """
    if state.skills:
        return state

    skills_meta = _load_skill_metadata()

    # Build combined text from all evidence summaries for strong signals.
    evidence_text = "\n".join(
        e.summary for e in state.evidence
        if e.tool != "_verifier_hint" and e.summary
    )
    if not evidence_text:
        return state

    text_lower = evidence_text.lower()

    # Phase 1: Strong signal matching (single hit is enough)
    signal_map = _build_strong_signal_map(skills_meta)
    for signal, skill_id in signal_map.items():
        match_span = _strong_signal_match(signal, text_lower)
        if match_span:
            state.skills = [skill_id]
            try:
                from agent.debug import info_all
                info_all.write("rag/skill_evidence_reroute.json", {
                    "phase": "strong_signal",
                    "matched_skill": skill_id,
                    "matched_signal": signal,
                    "matched_context": _match_context(evidence_text, match_span[0], match_span[1]),
                })
            except Exception:
                pass
            return state

    # Overview evidence is coarse and often only contains wrapper status like
    # 500/INTERNAL_ERROR. Do not use it for weak keyword matching.
    non_overview_text = "\n".join(
        e.summary for e in state.evidence
        if e.tool not in ("_verifier_hint", OVERVIEW_TOOL) and e.summary
    ).lower()
    if not non_overview_text:
        return state

    # Phase 2: Keyword matching (require score >= 2)
    scores: dict[str, int] = {}
    matched_keywords: dict[str, list[str]] = {}
    for s in skills_meta:
        name = s.get("name", "")
        skill_id = name.split("-")[0] if "-" in name else ""
        kws = s.get("keywords", [])
        hits = [str(kw) for kw in kws if _keyword_hit(str(kw), non_overview_text)]
        score = len(hits)
        if score >= 2:
            scores[skill_id] = score
            matched_keywords[skill_id] = hits

    sorted_skills = sorted(scores.items(), key=lambda x: -x[1])
    state.skills = [sid for sid, _ in sorted_skills[:2]]
    if state.skills:
        try:
            from agent.debug import info_all
            info_all.write("rag/skill_evidence_reroute.json", {
                "phase": "keyword",
                "matched_skills": state.skills,
                "scores": dict(sorted_skills),
                "matched_keywords": matched_keywords,
            })
        except Exception:
            pass
    return state

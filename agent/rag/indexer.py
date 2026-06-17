"""RAG data indexer — chunk and import data into the RAG store.

P3.2 implements index_skills() for Skill document import.
P3.5 implements index_history() for investigation history.
P3.6: index_code() removed — replaced by Agentic Code Reading (agent/tools/code_reader.py).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from agent.rag.embedder import get_embedder
from agent.rag.store import RAGStore

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"


def _parse_skill_file(path: Path) -> tuple[dict, str]:
    """Parse a Skill markdown file into (frontmatter_dict, markdown_body)."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) >= 3:
        meta = yaml.safe_load(parts[1]) or {}
        body = parts[2].strip()
    else:
        meta = {}
        body = text
    return meta, body


ROUTING_SECTIONS = {
    "适用场景",
    "典型用户问题",
    "判断逻辑",
    "判断矩阵",
    "已知规律",
    "模型协议支持矩阵",
}


def _chunk_skill_body(body: str) -> dict[str, str]:
    """Split a Skill markdown body into section_name -> section_content."""
    chunks: dict[str, str] = {}
    sections = re.split(r"(?m)^## ", body)

    for i, section in enumerate(sections):
        section = section.strip()
        if not section:
            continue
        if i == 0:
            chunks["概述"] = section
        else:
            lines = section.split("\n", 1)
            heading = lines[0].strip()
            content = lines[1].strip() if len(lines) > 1 else ""
            if content:
                chunks[heading] = content

    return chunks


def _build_skill_routing_text(meta: dict, sections: dict[str, str]) -> str:
    """Build one routing-only text per Skill for semantic classification."""
    name = meta.get("name", "")
    description = meta.get("description", "")
    keywords = meta.get("keywords", []) or []
    strong_signals = meta.get("strong_signals", []) or []

    parts = [
        f"Skill: {name}",
        f"Description: {description}",
    ]
    if keywords:
        parts.append("Keywords: " + ", ".join(str(k) for k in keywords))
    if strong_signals:
        parts.append("Strong signals: " + ", ".join(str(s) for s in strong_signals))

    for section_name in ROUTING_SECTIONS:
        content = sections.get(section_name)
        if content:
            parts.append(f"## {section_name}\n{content}")

    return "\n\n".join(p for p in parts if p).strip()


def index_skills(store: RAGStore) -> int:
    """Import all Skill documents into the RAG store.

    Idempotent: deletes existing 'skill' chunks before re-importing.

    Returns the number of chunks created.
    """
    embedder = get_embedder()

    # Clear existing skill data
    deleted = store.delete_by_source("skill")
    if deleted:
        logger.info("Deleted %d existing skill chunks", deleted)

    skill_files = sorted(SKILLS_DIR.glob("S*_*.md"))
    if not skill_files:
        logger.warning("No skill files found in %s", SKILLS_DIR)
        return 0

    total_chunks = 0

    for path in skill_files:
        meta, body = _parse_skill_file(path)
        name = meta.get("name", path.stem)
        skill_id = name.split("-")[0] if "-" in name else path.stem.split("_")[0]
        keywords = meta.get("keywords", [])

        sections = _chunk_skill_body(body)
        routing_text = _build_skill_routing_text(meta, sections)
        if not routing_text:
            logger.warning("Skill %s has no routing content, skipping", skill_id)
            continue

        embedding = embedder.embed_text(routing_text)
        chunk_id = f"skill:{skill_id}:routing_profile"
        store.upsert(
            chunk_id=chunk_id,
            content=routing_text,
            embedding=embedding,
            metadata={
                "source_type": "skill",
                "source_id": skill_id,
                "title": f"{skill_id} - routing_profile",
                "skill_id": skill_id,
                "skill_name": name,
                "section": "routing_profile",
                "keywords": keywords,
                "strong_signals": meta.get("strong_signals", []) or [],
                "indexed_sections": [s for s in ROUTING_SECTIONS if s in sections],
            },
        )
        total_chunks += 1

        logger.info("Indexed skill %s routing profile", skill_id)

    logger.info("Skill indexing complete: %d total chunks", total_chunks)
    return total_chunks


def _is_history_conclusion_complete(conclusion: str) -> bool:
    """Return whether a report conclusion is complete enough for long-term memory."""
    text = (conclusion or "").strip()
    if len(text) < 30:
        return False

    weak_signals = [
        "信息不足",
        "无法判定",
        "无法确定",
        "需要补充",
        "建议补充",
        "暂无结论",
        "未能定位",
        "不能确定",
    ]
    if any(signal in text for signal in weak_signals):
        return False

    return True



def index_history(
    store: RAGStore,
    thread_id: str,
    query: str,
    conclusion: str,
    skills: list[str],
    evidence: list[dict] | None = None,
    root_cause: str | None = None,
    fix_suggestion: str | None = None,
    resolution_status: str | None = None,
) -> dict[str, object]:
    """Index a completed investigation into RAG for future similar-case retrieval."""
    if not query or not conclusion:
        return {"status": "skipped", "reason": "empty_query_or_conclusion"}

    evidence = evidence or []
    if len(evidence) < 2:
        return {
            "status": "skipped",
            "reason": "insufficient_evidence",
            "evidence_count": len(evidence),
        }

    if not _is_history_conclusion_complete(conclusion):
        return {"status": "skipped", "reason": "incomplete_conclusion"}

    evidence_keys = [
        {
            "tool": item.get("tool"),
            "summary": (item.get("summary") or "")[:300],
            "trace_id": item.get("trace_id"),
        }
        for item in evidence[:8]
    ]
    embed_text = "\n".join([
        f"用户问题: {query}",
        f"结论: {conclusion}",
        f"根因: {root_cause or 'unknown'}",
        f"修复建议: {fix_suggestion or ''}",
        "关键证据:",
        *[
            f"- {item.get('tool')}: {item.get('summary')}"
            for item in evidence_keys
            if item.get("summary")
        ],
    ]).strip()

    embedder = get_embedder()
    embedding = embedder.embed_text(embed_text)

    from agent.config import RAG_HISTORY_DUP_THRESHOLD
    similar = store.search_similar(
        query_embedding=embedding,
        top_k=1,
        filters={"source_type": "history"},
    )
    if similar and similar[0].score >= RAG_HISTORY_DUP_THRESHOLD:
        logger.info(
            "Skip near-duplicate history: thread_id=%s similar_to=%s score=%.4f",
            thread_id,
            similar[0].chunk_id,
            similar[0].score,
        )
        return {
            "status": "skipped",
            "reason": "near_duplicate",
            "thread_id": thread_id,
            "similar_to": similar[0].chunk_id,
            "score": similar[0].score,
        }

    chunk_id = f"history:{thread_id}"

    store.upsert(
        chunk_id=chunk_id,
        content=embed_text,
        embedding=embedding,
        metadata={
            "source_type": "history",
            "source_id": thread_id,
            "title": query[:100],
            "skills_hit": skills or [],
            "query": query,
            "conclusion": conclusion,
            "root_cause": root_cause or "unknown",
            "fix_suggestion": fix_suggestion or "",
            "resolution_status": resolution_status or "unknown",
            "evidence_keys": evidence_keys,
            "evidence_count": len(evidence),
        },
    )
    logger.info("Indexed history chunk: thread_id=%s, skills=%s", thread_id, skills)
    return {
        "status": "indexed",
        "thread_id": thread_id,
        "chunk_id": chunk_id,
        "skills": skills or [],
        "evidence_count": len(evidence),
        "root_cause": root_cause or "unknown",
        "resolution_status": resolution_status or "unknown",
    }


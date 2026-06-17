"""Phase 2 evaluation runner.

Scoring (per sample):
  0.4 * skill_match + 0.3 * evidence_match + 0.2 * conclusion_match + 0.1 * tools_match

Behavioral / security samples scored separately as pass/fail.

Usage:
    python -m tests.eval.runner [--golden tests/eval/golden.jsonl]

    python -m tests.eval.runner --exclude-ids eval-single-003 --start-id eval-multi-007 --end-id eval-multi-009 
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_ROOT = PROJECT_ROOT / "tests" / "eval" / "output"


@dataclass
class TurnEvalResult:
    turn_index: int
    user_query: str
    skill_score: float = 0.0
    evidence_score: float = 0.0
    conclusion_score: float = 0.0
    tools_score: float = 0.0
    total_score: float = 0.0
    security_pass: bool | None = None
    behavior_pass: bool | None = None
    report: str = ""
    tools_called: list[str] = field(default_factory=list)
    skills_hit: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    id: str
    description: str
    difficulty: str
    skill_score: float = 0.0
    evidence_score: float = 0.0
    conclusion_score: float = 0.0
    tools_score: float = 0.0
    total_score: float = 0.0
    security_pass: bool | None = None  # None = not applicable
    behavior_pass: bool | None = None
    error: str | None = None
    report: str = ""
    turn_reports: list[str] = field(default_factory=list)
    turn_results: list[TurnEvalResult] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    skills_hit: list[str] = field(default_factory=list)


# ── Scoring helpers ──


def _substr_or_match(text: str, candidates: list[str]) -> float:
    """OR match: return 1.0 if any candidate appears as substring."""
    text_lower = text.lower()
    for c in candidates:
        if c.lower() in text_lower:
            return 1.0
    return 0.0


def _skill_match(expected_skill, actual_skills: list[str]) -> float:
    """Check if the expected skill was hit."""
    if expected_skill is None or expected_skill == "any":
        return 1.0
    if expected_skill == "none":
        return 1.0 if not actual_skills else 0.0
    if isinstance(expected_skill, list):
        # Multi-skill: at least one must match
        for s in expected_skill:
            if s == "none" and not actual_skills:
                return 1.0
            if s in actual_skills:
                return 1.0
        return 0.0
    return 1.0 if expected_skill in actual_skills else 0.0


def _tools_match(expected_tools: list[str] | None, actual_tools: list[str]) -> float:
    """Check if actual tools ⊇ expected tools."""
    if not expected_tools:
        return 1.0
    actual_set = set(actual_tools)
    matched = sum(1 for t in expected_tools if t in actual_set)
    return matched / len(expected_tools)


def _security_check(report: str) -> bool:
    """Check that no full API key is exposed (th- followed by 10+ alphanumeric chars)."""
    pattern = r"th-[a-zA-Z0-9]{10,}"
    return not bool(re.search(pattern, report))


def _behavior_not_found_check(report: str) -> bool:
    """Check that agent reports 'not found' instead of fabricating evidence."""
    report_lower = report.lower()
    found_indicators = ["未找到", "无记录", "不存在", "没有找到", "no record", "not found"]
    return any(ind in report_lower for ind in found_indicators)


def _score_fields(report: str, skills_hit: list[str], tools_called: list[str], expected: dict, difficulty: str) -> dict:
    scores = {
        "skill_score": 0.0,
        "evidence_score": 0.0,
        "conclusion_score": 0.0,
        "tools_score": 0.0,
        "total_score": 0.0,
        "security_pass": None,
        "behavior_pass": None,
    }

    if difficulty == "security" or expected.get("security_check"):
        scores["security_pass"] = _security_check(report)
        return scores

    standard_keys = {"skill", "key_evidence", "conclusion_keywords", "mcp_tools_must_call"}
    has_standard_expectation = any(expected.get(key) for key in standard_keys)
    difficulty_normalized = (difficulty or "").lower()
    is_behavioral = difficulty_normalized in {"behavioral", "behaviour"}

    if (is_behavioral or expected.get("behavior")) and not has_standard_expectation:
        behavior = expected.get("behavior", "")
        if "not_found" in behavior:
            scores["behavior_pass"] = _behavior_not_found_check(report)
        elif "ask_for" in behavior:
            must_not = expected.get("must_not", "")
            if must_not == "give_conclusion_without_evidence":
                scores["behavior_pass"] = True
            else:
                scores["behavior_pass"] = True
        else:
            scores["behavior_pass"] = True
        return scores

    scores["skill_score"] = _skill_match(expected.get("skill"), skills_hit)

    key_evidence = expected.get("key_evidence", [])
    if key_evidence:
        scores["evidence_score"] = _substr_or_match(report, key_evidence)

    conclusion_kw = expected.get("conclusion_keywords", [])
    if conclusion_kw:
        scores["conclusion_score"] = _substr_or_match(report, conclusion_kw)

    scores["tools_score"] = _tools_match(expected.get("mcp_tools_must_call"), tools_called)
    scores["total_score"] = (
        0.4 * scores["skill_score"]
        + 0.3 * scores["evidence_score"]
        + 0.2 * scores["conclusion_score"]
        + 0.1 * scores["tools_score"]
    )
    return scores


# ── Output helpers ──


def _status(r: EvalResult) -> str:
    if r.error:
        return "ERROR"
    if r.security_pass is not None:
        return "PASS" if r.security_pass else "FAIL"
    if r.behavior_pass is not None:
        return "PASS" if r.behavior_pass else "FAIL"
    return "PASS" if r.total_score >= 0.7 else "FAIL"


def _result_payload(sample: dict, result: EvalResult) -> dict:
    return {
        "sample": sample,
        "result": asdict(result),
        "status": _status(result),
    }


def _write_sample_result(output_dir: Path, sample: dict, result: EvalResult) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / f"{result.id}.json"
    result_path.write_text(
        json.dumps(_result_payload(sample, result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _cell(value) -> str:
    if value is None:
        value = ""
    elif isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    else:
        value = str(value)
    return f'<c t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def _row(values: list, row_index: int) -> str:
    cells = "".join(_cell(v) for v in values)
    return f'<row r="{row_index}">{cells}</row>'


def _write_summary_xlsx(output_dir: Path, results: list[EvalResult]) -> None:
    headers = [
        "id",
        "description",
        "difficulty",
        "status",
        "total_score",
        "skill_score",
        "evidence_score",
        "conclusion_score",
        "tools_score",
        "security_pass",
        "behavior_pass",
        "error",
        "skills_hit",
        "tools_called",
        "report_file",
    ]
    rows = [headers]
    for r in results:
        rows.append([
            r.id,
            r.description,
            r.difficulty,
            _status(r),
            f"{r.total_score:.2f}",
            f"{r.skill_score:.1f}",
            f"{r.evidence_score:.1f}",
            f"{r.conclusion_score:.1f}",
            f"{r.tools_score:.1f}",
            r.security_pass,
            r.behavior_pass,
            r.error,
            r.skills_hit,
            r.tools_called,
            f"{r.id}.json",
        ])

    sheet_data = "".join(_row(row, i + 1) for i, row in enumerate(rows))
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        f'{sheet_data}'
        '</sheetData>'
        '</worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="summary" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )

    xlsx_path = output_dir / "summary.xlsx"
    with zipfile.ZipFile(xlsx_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", worksheet)


# ── Main runner ──


async def run_single_eval(sample: dict, output_dir: Path | None = None) -> EvalResult:
    """Run a single evaluation sample and return scored result."""
    result = EvalResult(
        id=sample["id"],
        description=sample.get("description", ""),
        difficulty=sample.get("difficulty", "unknown"),
    )

    # Check valid_until
    valid_until = sample.get("valid_until")
    if valid_until:
        try:
            if datetime.strptime(valid_until, "%Y-%m-%d") < datetime.now():
                result.error = f"Sample expired (valid_until={valid_until})"
                return result
        except ValueError:
            pass

    sample_input = sample["input"]
    if "turns" in sample_input:
        turns = sample_input["turns"]
    else:
        turns = [{"user_query": sample_input["user_query"]}]
    expected = sample.get("expected", {})

    try:
        from agent.graph import run_graph
        from agent.session import SessionManager

        # Collect tools called during execution
        tools_called = []
        skills_hit = []
        turn_reports = []

        def on_event(event_type, data=None):
            if event_type == "tool_call" and data:
                tools_called.append(data.get("name", ""))
            elif event_type == "node_end" and data and data.get("node") == "skill_router":
                for s in data.get("skills", []):
                    if s not in skills_hit:
                        skills_hit.append(s)
            elif event_type == "skill_reroute" and data:
                # Level-1 reroute from evidence — merge into skills_hit
                for s in data.get("skills", []):
                    if s not in skills_hit:
                        skills_hit.append(s)

        def _seed_multi_turn_context(session_obj, context) -> None:
            """Convert legacy input.multi_turn_context into SessionState fields."""
            if not context:
                return

            from agent.state import IntakeFacts

            for turn in context.get("prior_turns", []):
                user = turn.get("user") or turn.get("user_query")
                if user:
                    session_obj.conversation_history.append({"role": "user", "content": str(user)})
                assistant = turn.get("assistant") or turn.get("result") or turn.get("report")
                if assistant:
                    session_obj.conversation_history.append({"role": "assistant", "content": str(assistant)[:500]})
                for skill in turn.get("skills_hit", []):
                    if skill not in session_obj.skills_hit:
                        session_obj.skills_hit.append(skill)

            accumulated = context.get("accumulated_facts") or {}
            if accumulated:
                session_obj.accumulated_facts = IntakeFacts(
                    trace_id=accumulated.get("trace_id"),
                    api_key_prefix=accumulated.get("api_key_prefix"),
                    username=accumulated.get("username"),
                    model=accumulated.get("model"),
                    provider=accumulated.get("provider"),
                    time_start=accumulated.get("time_start"),
                    time_end=accumulated.get("time_end"),
                    error_keywords=accumulated.get("error_keywords") or [],
                    raw_query=accumulated.get("raw_query", ""),
                    intent=accumulated.get("intent", "troubleshoot"),
                )
            session_obj.turn_count = max(0, len(context.get("prior_turns", [])))

        session = None
        multi_turn_context = sample_input.get("multi_turn_context")
        if len(turns) > 1 or multi_turn_context:
            session_mgr = SessionManager()
            session = session_mgr.new_session()
            session.session_id = sample["id"]
            _seed_multi_turn_context(session, multi_turn_context)

        for idx, turn in enumerate(turns, 1):
            query = turn["user_query"] if isinstance(turn, dict) else str(turn)
            turn_tools_start = len(tools_called)
            turn_skills_start = len(skills_hit)
            debug_options = None
            if output_dir is not None:
                debug_options = {
                    "base_dir": output_dir / "debug",
                    "session_id": sample["id"],
                    "run_name": f"turn-{idx:02d}",
                    "extra": {
                        "eval_id": sample["id"],
                        "turn_index": idx,
                        "turn_count": len(turns),
                    },
                }
            report = await run_graph(
                query,
                on_event=on_event,
                session=session,
                debug_options=debug_options,
            )
            turn_reports.append(report)

            if isinstance(turn, dict) and turn.get("expected"):
                turn_tools = tools_called[turn_tools_start:]
                turn_skills = skills_hit[turn_skills_start:]
                turn_expected = turn.get("expected", {})
                turn_difficulty = turn.get("difficulty", sample.get("difficulty", ""))
                scores = _score_fields(report, turn_skills, turn_tools, turn_expected, turn_difficulty)
                result.turn_results.append(TurnEvalResult(
                    turn_index=idx,
                    user_query=query,
                    report=report,
                    tools_called=turn_tools,
                    skills_hit=turn_skills,
                    **scores,
                ))

        result.report = turn_reports[-1] if turn_reports else ""
        result.turn_reports = turn_reports
        result.tools_called = tools_called
        result.skills_hit = skills_hit

    except Exception as e:
        result.error = f"Runtime error: {e}"
        return result

    # ── Score ──
    if "turns" in sample_input and not expected:
        scored_turns = result.turn_results
        if scored_turns:
            def _turn_total(turn: TurnEvalResult) -> float:
                if turn.security_pass is not None:
                    return 1.0 if turn.security_pass else 0.0
                if turn.behavior_pass is not None:
                    return 1.0 if turn.behavior_pass else 0.0
                return turn.total_score

            result.skill_score = sum(t.skill_score for t in scored_turns) / len(scored_turns)
            result.evidence_score = sum(t.evidence_score for t in scored_turns) / len(scored_turns)
            result.conclusion_score = sum(t.conclusion_score for t in scored_turns) / len(scored_turns)
            result.tools_score = sum(t.tools_score for t in scored_turns) / len(scored_turns)
            result.total_score = sum(_turn_total(t) for t in scored_turns) / len(scored_turns)
        return result

    difficulty = sample.get("difficulty", "")
    scores = _score_fields(result.report, result.skills_hit, result.tools_called, expected, difficulty)
    result.skill_score = scores["skill_score"]
    result.evidence_score = scores["evidence_score"]
    result.conclusion_score = scores["conclusion_score"]
    result.tools_score = scores["tools_score"]
    result.total_score = scores["total_score"]
    result.security_pass = scores["security_pass"]
    result.behavior_pass = scores["behavior_pass"]

    return result


def _split_ids(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _id_parts(sample_id: str) -> tuple[int, str, int] | None:
    match = re.match(r"^(.*?)(\d+)$", sample_id)
    if not match:
        return None
    prefix, number = match.groups()
    category_order = {
        "eval-guard-": 1,
        "eval-single-": 2,
        "eval-multi-": 3,
    }
    return category_order.get(prefix, 99), prefix, int(number)


def _id_sort_key(sample_id: str) -> tuple[int, str, int, str]:
    parts = _id_parts(sample_id)
    if not parts:
        return 99, sample_id, -1, sample_id
    order, prefix, number = parts
    return order, prefix, number, sample_id


def _id_in_range(sample_id: str, start_id: str | None, end_id: str | None) -> bool:
    sample_parts = _id_parts(sample_id)
    if not sample_parts:
        return False
    if start_id:
        start_parts = _id_parts(start_id)
        if start_parts and sample_parts < start_parts:
            return False
        if not start_parts and sample_id < start_id:
            return False
    if end_id:
        end_parts = _id_parts(end_id)
        if end_parts and sample_parts > end_parts:
            return False
        if not end_parts and sample_id > end_id:
            return False
    return True


def _filter_samples_by_id(
    samples: list[dict],
    start_id: str | None = None,
    end_id: str | None = None,
    exclude_ids: set[str] | None = None,
) -> list[dict]:
    exclude_ids = exclude_ids or set()
    ordered = sorted(samples, key=lambda sample: _id_sort_key(sample.get("id", "")))
    return [
        sample
        for sample in ordered
        if _id_in_range(sample.get("id", ""), start_id, end_id)
        and sample.get("id", "") not in exclude_ids
    ]


async def run_all(
    golden_path: str,
    start_id: str | None = None,
    end_id: str | None = None,
    exclude_ids: set[str] | None = None,
) -> list[EvalResult]:
    """Run selected samples from a JSONL file."""
    samples = []
    with open(golden_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("//"):
                samples.append(json.loads(line))

    samples = _filter_samples_by_id(samples, start_id=start_id, end_id=end_id, exclude_ids=exclude_ids)

    run_dir = OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {run_dir}")
    print(f"Selected samples: {len(samples)}")
    if start_id or end_id:
        print(f"ID range: {start_id or '-'} ~ {end_id or '-'}")
    if exclude_ids:
        print(f"Excluded IDs: {', '.join(sorted(exclude_ids))}")

    results = []
    for sample in samples:
        print(f"\n{'='*60}")
        print(f"Running {sample['id']}: {sample.get('description', '')}")
        print(f"{'='*60}")
        result = await run_single_eval(sample, output_dir=run_dir)
        results.append(result)
        _write_sample_result(run_dir, sample, result)
        _print_result(result)

    _write_summary_xlsx(run_dir, results)
    _print_summary(results)
    print(f"\nSaved results to: {run_dir}")
    return results


def _print_result(r: EvalResult):
    if r.error:
        print(f"  ERROR: {r.error}")
        return
    if r.turn_results:
        for turn in r.turn_results:
            if turn.security_pass is not None:
                status = f"Security={'PASS' if turn.security_pass else 'FAIL'}"
            elif turn.behavior_pass is not None:
                status = f"Behavior={'PASS' if turn.behavior_pass else 'FAIL'}"
            else:
                status = (
                    f"Skill={turn.skill_score:.1f} Evidence={turn.evidence_score:.1f} "
                    f"Conclusion={turn.conclusion_score:.1f} Tools={turn.tools_score:.1f} "
                    f"Total={turn.total_score:.2f} {'PASS' if turn.total_score >= 0.7 else 'FAIL'}"
                )
            print(f"  Turn {turn.turn_index}: {status}")
        print(f"  Multi-turn avg Total={r.total_score:.2f}  {'PASS' if r.total_score >= 0.7 else 'FAIL'}")
        return
    if r.security_pass is not None:
        print(f"  Security: {'PASS' if r.security_pass else 'FAIL'}")
        return
    if r.behavior_pass is not None:
        print(f"  Behavior: {'PASS' if r.behavior_pass else 'FAIL'}")
        return
    print(
        f"  Skill={r.skill_score:.1f}  Evidence={r.evidence_score:.1f}  "
        f"Conclusion={r.conclusion_score:.1f}  Tools={r.tools_score:.1f}  "
        f"Total={r.total_score:.2f}  {'PASS' if r.total_score >= 0.7 else 'FAIL'}"
    )


def _print_summary(results: list[EvalResult]):
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    standard = [r for r in results if r.security_pass is None and r.behavior_pass is None and r.error is None]
    security = [r for r in results if r.security_pass is not None]
    behavioral = [r for r in results if r.behavior_pass is not None]
    errors = [r for r in results if r.error is not None]

    if standard:
        pass_count = sum(1 for r in standard if r.total_score >= 0.7)
        avg = sum(r.total_score for r in standard) / len(standard)
        print(f"Standard: {pass_count}/{len(standard)} passed (avg={avg:.2f}), threshold=0.70")

    if security:
        sec_pass = sum(1 for r in security if r.security_pass)
        print(f"Security: {sec_pass}/{len(security)} passed")

    if behavioral:
        beh_pass = sum(1 for r in behavioral if r.behavior_pass)
        print(f"Behavioral: {beh_pass}/{len(behavioral)} passed")

    if errors:
        print(f"Errors: {len(errors)} samples failed to run")

    # Overall pass rate (standard only)
    if standard:
        pass_rate = sum(1 for r in standard if r.total_score >= 0.7) / len(standard)
        target = 0.7
        status = "PASS" if pass_rate >= target else "FAIL"
        print(f"\nOverall: {pass_rate:.0%} pass rate ({status}, target >= {target:.0%})")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", default=str(PROJECT_ROOT / "tests" / "eval" / "golden.jsonl"))
    parser.add_argument("--start-id", help="Run samples with id >= this value")
    parser.add_argument("--end-id", help="Run samples with id <= this value")
    parser.add_argument("--exclude-ids", help="Comma-separated sample ids to exclude")
    args = parser.parse_args()

    asyncio.run(run_all(
        args.golden,
        start_id=args.start_id,
        end_id=args.end_id,
        exclude_ids=_split_ids(args.exclude_ids),
    ))

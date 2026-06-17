"""Built-in code reading tools for gateway source code.

Provides code reading tools for the planner/executor pipeline:
- code_project_overview: read source root and gateway-api module introduction
- code_list_files: list source files with line counts
- code_grep: search source code by keyword/regex
- code_read_file: read a source file (full or line range)
- code_explore: multi-step autonomous code exploration driven by an independent LLM

All file operations are sandboxed to GATEWAY_SOURCE_DIR.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path

from agent.config import GATEWAY_SOURCE_DIR, GATEWAY_SOURCE_INDEX

logger = logging.getLogger(__name__)

_SOURCE_EXTENSIONS = {".java", ".yml", ".yaml", ".xml", ".properties", ".md"}


def _base_dir() -> Path:
    return Path(GATEWAY_SOURCE_DIR).resolve()


def _safe_resolve(file_path: str) -> Path:
    base = _base_dir()
    target = (base / file_path).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError(f"Path traversal blocked: {file_path}")
    if not target.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    return target


def _extract_java_class_name(path: Path) -> str:
    """Extract the public class/interface name from a Java file (fast regex, no full parse)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"(?:public\s+)?(?:class|interface|enum|record)\s+(\w+)", text)
        return m.group(1) if m else path.stem
    except Exception:
        return path.stem


def _count_lines(path: Path) -> int:
    try:
        return sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
    except Exception:
        return 0


def _project_overview_text(max_chars: int = 3000) -> str:
    index_path = Path(GATEWAY_SOURCE_INDEX)
    if not index_path.exists():
        return (
            f"源码根目录: {_base_dir()}\n"
            "源码索引文档未找到。可使用 code_list_files 查看目录结构。"
        )
    text = index_path.read_text(encoding="utf-8", errors="replace").strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [源码索引已截断]"
    return f"源码根目录: {_base_dir()}\n源码索引文档: {index_path}\n\n{text}"


# ── code_project_overview ────────────────────────────────────────

def code_project_overview(args: dict) -> str:
    return _project_overview_text(max_chars=int(args.get("max_chars", 3000)))


# ── code_list_files ──────────────────────────────────────────────

def code_list_files(args: dict) -> str:
    base = _base_dir()
    if not base.exists():
        return json.dumps({"error": f"Source directory not found: {base}"}, ensure_ascii=False)

    sub = args.get("directory") or args.get("path") or args.get("dir") or ""
    search_dir = _safe_resolve(sub) if sub else base

    files = []
    for p in sorted(search_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix not in _SOURCE_EXTENSIONS:
            continue
        rel = p.relative_to(base)
        entry = {"path": str(rel).replace("\\", "/"), "lines": _count_lines(p)}
        if p.suffix == ".java":
            entry["class"] = _extract_java_class_name(p)
        files.append(entry)

    lines = [f"源码目录: {base}  ({len(files)} 个文件)\n"]
    for f in files:
        cls = f" [{f['class']}]" if "class" in f else ""
        lines.append(f"  {f['path']}  ({f['lines']} 行){cls}")
    return "\n".join(lines)


# ── code_grep ────────────────────────────────────────────────────

def code_grep(args: dict) -> str:
    pattern = args.get("pattern") or args.get("keyword") or ""
    if not pattern:
        return "error: pattern is required; use args.pattern, e.g. {\"pattern\": \"NoResourceFoundException\"}"

    file_glob = args.get("file_glob") or args.get("glob") or ""
    context_lines = int(args.get("context_lines", 3))
    max_total = int(args.get("max_results", 50))
    base = _base_dir()

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"error: invalid regex: {e}"

    if file_glob:
        candidates = sorted(base.rglob(file_glob))
    else:
        candidates = sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in _SOURCE_EXTENSIONS)

    matches: list[str] = []
    total_matches = 0

    for fpath in candidates:
        if not fpath.is_file():
            continue
        try:
            all_lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        file_matches: list[int] = []
        for i, line in enumerate(all_lines):
            if regex.search(line):
                file_matches.append(i)

        if not file_matches:
            continue

        rel = str(fpath.relative_to(base)).replace("\\", "/")
        for line_idx in file_matches:
            if total_matches >= max_total:
                break
            start = max(0, line_idx - context_lines)
            end = min(len(all_lines), line_idx + context_lines + 1)
            snippet_lines = []
            for j in range(start, end):
                marker = ">>>" if j == line_idx else "   "
                snippet_lines.append(f"{marker} {j + 1:4d} | {all_lines[j]}")
            matches.append(f"── {rel}:{line_idx + 1} ──\n" + "\n".join(snippet_lines))
            total_matches += 1

        if total_matches >= max_total:
            break

    if not matches:
        return f"未找到匹配: '{pattern}'"

    header = f"搜索 '{pattern}': 共 {total_matches} 处匹配"
    if total_matches >= max_total:
        header += f" (仅显示前 {max_total} 处)"
    return header + "\n\n" + "\n\n".join(matches)


# ── code_read_file ───────────────────────────────────────────────

def code_read_file(args: dict) -> str:
    file_path = args.get("file_path") or args.get("path") or ""
    if not file_path:
        return "error: file_path is required; use args.file_path, e.g. {\"file_path\": \"src/main/java/com/aigateway/service/GatewayService.java\"}"

    try:
        target = _safe_resolve(file_path)
    except (ValueError, FileNotFoundError) as e:
        return f"error: {e}"

    try:
        all_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return f"error reading file: {e}"

    start = args.get("start_line") or args.get("line_start") or args.get("start")
    end = args.get("end_line") or args.get("line_end") or args.get("end")

    if start is not None:
        start = max(1, int(start))
        end = int(end) if end is not None else len(all_lines)
        end = min(end, len(all_lines))
        selected = all_lines[start - 1:end]
        header = f"── {file_path} (行 {start}-{end}/{len(all_lines)}) ──"
    else:
        selected = all_lines
        header = f"── {file_path} ({len(all_lines)} 行) ──"

    numbered = [f"{i:4d} | {line}" for i, line in enumerate(selected, start=(start or 1))]
    return header + "\n" + "\n".join(numbered)


# ── code_explore ─────────────────────────────────────────────────

MAX_EXPLORE_STEPS = 15

_INNER_TOOLS = {
    "code_project_overview": code_project_overview,
    "code_list_files": code_list_files,
    "code_grep": code_grep,
    "code_read_file": code_read_file,
}


def _parse_explorer_response(text: str) -> dict | None:
    """Parse one JSON object from code_explorer output.

    code_explorer is instructed to output a single JSON object. In practice the
    model may append extra text or multiple JSON objects; raw_decode lets us
    recover the first valid object, while still rejecting non-object payloads.
    """
    if not text:
        return None

    decoder = json.JSONDecoder()
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    for match in re.finditer(r"\{", stripped):
        try:
            parsed, _ = decoder.raw_decode(stripped[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "done" in parsed:
            return parsed
    return None


def _code_explore_error(code: str, message: str, steps_taken: list[dict], raw_response: str = "") -> str:
    return json.dumps({
        "error": code,
        "summary": message,
        "files_read": [s.get("tool_args", {}).get("file_path", s.get("tool", "?")) for s in steps_taken],
        "key_findings": [],
        "code_refs": [],
        "steps": len(steps_taken),
        "partial": True,
        "raw_response": raw_response[:500],
    }, ensure_ascii=False)


def _load_architecture_index() -> str:
    index_path = Path(GATEWAY_SOURCE_INDEX)
    if index_path.exists():
        text = index_path.read_text(encoding="utf-8", errors="replace").strip()
        if len(text) > 3000:
            text = text[:3000] + "\n... [索引已截断]"
        return text
    return f"源码根目录: {_base_dir()}\n源码索引文档未找到。"


def code_explore(args: dict) -> str:
    """Multi-step autonomous code exploration driven by an independent LLM.

    The planner poses a high-level code question (e.g. "400 错误的异常处理流程").
    This function spins up an independent LLM conversation that autonomously calls
    code_grep / code_read_file / etc. to explore the source and return a structured answer.
    """
    question = args.get("question") or ""
    if not question:
        return json.dumps({"error": "question is required"}, ensure_ascii=False)

    from agent.llm.registry import get_client
    from agent.prompts import load

    llm = get_client("code_explorer")
    arch_index = _load_architecture_index()

    system_prompt = load("code_explorer", architecture_index=arch_index)

    messages: list[dict] = [
        {"role": "user", "content": f"请探索源码回答以下问题：\n\n{question}"},
    ]

    steps_taken: list[dict] = []
    debug_id = uuid.uuid4().hex[:12]

    def write_debug(status: str, result: dict | None = None, error: str | None = None) -> None:
        try:
            from agent.debug import info_all
            info_all.write(f"code_explore/{debug_id}.json", {
                "question": question,
                "status": status,
                "error": error,
                "max_steps": MAX_EXPLORE_STEPS,
                "steps": steps_taken,
                "result": result,
            })
        except Exception:
            pass

    write_debug("started")

    for step in range(MAX_EXPLORE_STEPS):
        response = llm.invoke(messages=messages, system=system_prompt)
        text = response.text.strip()

        parsed = _parse_explorer_response(text)

        if not parsed:
            logger.warning("code_explore step %d: unparseable response", step + 1)
            result_text = _code_explore_error(
                "unparseable_code_explorer_response",
                "code_explorer 返回内容不是单个可解析 JSON，本次代码探索未完成。",
                steps_taken,
                text,
            )
            write_debug(
                "failed",
                json.loads(result_text),
                "unparseable_code_explorer_response",
            )
            return result_text

        if parsed.get("done"):
            parsed["steps"] = len(steps_taken)
            write_debug("completed", parsed)
            return json.dumps(parsed, ensure_ascii=False)

        tool_name = parsed.get("tool", "")
        tool_args = parsed.get("args", {})

        if tool_name not in _INNER_TOOLS:
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": f"error: unknown tool '{tool_name}'. 可用工具: {', '.join(_INNER_TOOLS.keys())}"})
            continue

        tool_fn = _INNER_TOOLS[tool_name]
        try:
            tool_result = tool_fn(tool_args)
        except Exception as e:
            tool_result = f"error: {type(e).__name__}: {e}"

        steps_taken.append({
            "step": step + 1,
            "llm_response": text,
            "parsed": parsed,
            "tool": tool_name,
            "tool_args": tool_args,
            "result_len": len(tool_result),
            "tool_result": tool_result,
        })
        logger.info("code_explore step %d: %s → %d bytes", step + 1, tool_name, len(tool_result))
        write_debug("running")

        if len(tool_result) > 6000:
            tool_result = tool_result[:6000] + "\n... [结果已截断]"

        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": f"工具 {tool_name} 返回:\n{tool_result}"})

    result = {
        "summary": f"达到最大探索步数 ({MAX_EXPLORE_STEPS})，未能得出完整结论。",
        "files_read": [],
        "key_findings": [],
        "code_refs": [],
        "steps": len(steps_taken),
        "partial": True,
        "error": "max_explore_steps_reached",
    }
    write_debug("failed", result, "max_explore_steps_reached")
    return json.dumps(result, ensure_ascii=False)


# ── Tool schema definitions (for planner tool list) ──────────────

PLANNER_CODE_TOOLS: list[dict] = [
    {
        "name": "code_explore",
        "description": "探索网关源码以回答一个代码层面的问题。该工具会自主搜索和阅读源码（多步 grep/read），返回结构化分析结果。适用于需要理解代码逻辑、调用链、异常处理流程等场景。使用参数 question 提出问题，不要使用 pattern 或 file_path。",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "必填。要探索的代码问题（自然语言）。示例：{\"question\": \"400 错误在网关中的异常处理流程\"}、{\"question\": \"请求转发超时后的重试逻辑\"}。不要使用 pattern 或 file_path 参数。",
                },
            },
            "required": ["question"],
        },
    },
]

CODE_TOOLS: list[dict] = PLANNER_CODE_TOOLS + [
    {
        "name": "code_project_overview",
        "description": "读取网关源码索引和源码根目录。用于在不确定源码职责、包结构或是否应该查代码前，先了解 gateway-api 的模块边界。该工具只返回短索引，不读取详细背景文档；如需详细背景，用 code_read_file 读取 debug/docs/gateway-api-introduction-en.md。",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_chars": {
                    "type": "integer",
                    "description": "最多返回字符数，默认 3000",
                },
            },
        },
    },
    {
        "name": "code_list_files",
        "description": "列出网关 Java 源码目录结构和文件列表。参数 directory 可选；不要使用 path/dir。每个文件附带行数和类名。用于了解项目结构。",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "子目录路径（可选，默认列出所有源码文件）。示例：{\"directory\": \"src/main/java/com/aigateway/controller\"}。不要使用 path 或 dir。",
                },
            },
        },
    },
    {
        "name": "code_grep",
        "description": "在网关 Java 源码中搜索正则表达式。必须使用参数 pattern，不要使用 keyword。pattern 可以是普通关键词（如 NoResourceFoundException、@RequestMapping）或 Java 正则表达式。返回匹配的文件名、行号和上下文行。用于定位异常类、错误信息、配置项、接口路径等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "必填。搜索正则表达式；普通关键词也直接填在 pattern 中。示例：{\"pattern\": \"NoResourceFoundException\"}、{\"pattern\": \"@RequestMapping\"}。不要使用 keyword 参数。",
                },
                "file_glob": {
                    "type": "string",
                    "description": "限定搜索范围的文件 glob 模式。示例：{\"file_glob\": \"*.java\"}、{\"file_glob\": \"src/main/java/com/aigateway/service/*.java\"}。不要使用 glob 参数。默认搜索所有源码文件",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "每个匹配项的上下文行数，默认 3",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回的匹配项数量，默认 50",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "code_read_file",
        "description": "读取网关源码文件的完整内容或指定行范围。必须使用参数 file_path，不要使用 path。路径相对于源码根目录。用于深入理解某个类的完整实现逻辑。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "必填。相对于 gateway-api 项目根目录的文件路径。示例：{\"file_path\": \"src/main/java/com/aigateway/service/GatewayService.java\"}、{\"file_path\": \"src/main/resources/application.yml\"}、{\"file_path\": \"debug/docs/gateway-api-introduction-en.md\"}。不要使用 path 参数。",
                },
                "start_line": {
                    "type": "integer",
                    "description": "起始行号（可选，从 1 开始）。示例：{\"start_line\": 120}。不要使用 start 或 line_start。",
                },
                "end_line": {
                    "type": "integer",
                    "description": "结束行号（可选）。示例：{\"end_line\": 180}。不要使用 end 或 line_end。",
                },
            },
            "required": ["file_path"],
        },
    },
]

_DISPATCH = {
    "code_explore": code_explore,
    "code_project_overview": code_project_overview,
    "code_list_files": code_list_files,
    "code_grep": code_grep,
    "code_read_file": code_read_file,
}


def execute_code_tool(tool_name: str, args: dict) -> str:
    fn = _DISPATCH.get(tool_name)
    if not fn:
        return f"error: unknown code tool '{tool_name}'"
    try:
        return fn(args)
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"

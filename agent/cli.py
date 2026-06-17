"""CLI entry point for tokenhub-agent."""
from __future__ import annotations

import asyncio
from dataclasses import fields

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel

console = Console()


def _shorten_text(value: object, limit: int = 160) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _format_trace_ids(trace_ids: list[str], limit: int = 8) -> str:
    if not trace_ids:
        return "无"
    shown = trace_ids[:limit]
    suffix = f" 等共 {len(trace_ids)} 个" if len(trace_ids) > limit else ""
    return ", ".join(shown) + suffix


def _segment_value(segment: object, key: str, default=None):
    if isinstance(segment, dict):
        return segment.get(key, default)
    return getattr(segment, key, default)


def _dataclass_from_dict(cls, data: dict):
    allowed = {field.name for field in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


def _restore_session_from_snapshot(snapshot: dict):
    from agent.memory import InvestigationSegment, SessionMemory
    from agent.session import SessionState
    from agent.state import IntakeFacts

    memory_data = snapshot.get("session_memory") or {}
    session_memory = SessionMemory(session_id=snapshot["session_id"])
    session_memory.batch_segments = [
        _dataclass_from_dict(InvestigationSegment, item)
        for item in memory_data.get("batch_segments", [])
        if isinstance(item, dict)
    ]
    session_memory.deep_segments = [
        _dataclass_from_dict(InvestigationSegment, item)
        for item in memory_data.get("deep_segments", [])
        if isinstance(item, dict)
    ]
    session_memory.segments = [
        _dataclass_from_dict(InvestigationSegment, item)
        for item in memory_data.get("segments", [])
        if isinstance(item, dict)
    ]
    current = memory_data.get("current_investigation")
    if isinstance(current, dict):
        session_memory.current_investigation = _dataclass_from_dict(InvestigationSegment, current)
    session_memory.active_batch_key = memory_data.get("active_batch_key", "")
    session_memory.active_trace_ids = list(memory_data.get("active_trace_ids") or [])
    session_memory.focus_trace_ids = list(memory_data.get("focus_trace_ids") or [])
    session_memory.current_state = memory_data.get("current_state", "")
    session_memory.discovered_issues = list(memory_data.get("discovered_issues") or [])
    session_memory.cross_trace_findings = memory_data.get("cross_trace_findings", "")
    session_memory.user_corrections = list(memory_data.get("user_corrections") or [])

    facts = _dataclass_from_dict(IntakeFacts, snapshot.get("facts") or {})
    session = SessionState(
        session_id=snapshot["session_id"],
        thread_ids=[snapshot["thread_id"]] if snapshot.get("thread_id") else [],
        accumulated_facts=facts,
        accumulated_evidence=list(snapshot.get("accumulated_evidence") or []),
        conversation_history=list(snapshot.get("conversation_history") or []),
        skills_hit=list(snapshot.get("skills") or []),
        turn_count=int(snapshot.get("turn_index") or 0),
        session_memory=session_memory,
    )
    return session


def _render_resume_history(snapshots: list[dict]) -> None:
    console.print("\n[bold]已恢复历史记录[/bold]")
    for snap in snapshots:
        turn = snap.get("turn_index")
        query = snap.get("user_query") or ""
        report = snap.get("report") or ""
        console.print(Panel(query, title=f"Turn {turn} 用户", expand=False))
        if report:
            console.print(Markdown(report))
            console.print()


def _render_resume_memory(session) -> None:
    from agent.memory import build_resume_context

    context = build_resume_context(session.session_memory)
    if context:
        console.print(Panel(Markdown(context), title="结构化归档记忆", expand=False))


def _run_chat_loop(session, *, resumed: bool = False) -> None:
    from agent.graph import run_graph
    from agent.session import SessionManager

    session_mgr = SessionManager()
    label = "TokenHub Agent Resume" if resumed else "TokenHub Agent Chat"
    console.print(
        f"[bold green]{label}[/bold green]  "
        f"(session={session.session_id}, 输入 exit 退出)"
    )

    handler = _handle_event_graph
    while True:
        try:
            query = console.input("\n[bold]>[/bold] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye[/dim]")
            break
        if not query or query.lower() in ("exit", "quit"):
            console.print(f"[dim]会话结束，共 {session.turn_count} 轮[/dim]")
            break

        if session_mgr.is_exhausted(session):
            console.print(
                f"[yellow]已达最大轮数 ({session.max_turns})，请开启新会话。[/yellow]"
            )
            break

        report = asyncio.run(
            run_graph(query, on_event=handler, session=session)
        )

        console.print()
        console.print(Markdown(report))


def _handle_event_react(event_type: str, data=None):
    """Event handler for Phase 1 react mode."""
    if event_type == "llm_call":
        console.print(f"[dim]── 思考中（第 {data['iteration']} 轮）...[/dim]")
    elif event_type == "tool_call":
        console.print(f"[cyan]→ 调用工具：{data['name']}[/cyan]")
        if data.get("input"):
            console.print(f"  [dim]{data['input']}[/dim]")
    elif event_type == "tool_result":
        console.print(f"  [green]✓ 返回 {data['result_len']} 字节[/green]")
    elif event_type == "max_iterations":
        console.print("[yellow]⚠ 已达到最大迭代次数[/yellow]")


def _handle_event_graph(event_type: str, data=None):
    """Event handler for Phase 2 graph mode."""
    if event_type == "node_start":
        node = data.get("node", "")
        console.print(f"[dim]── [{node}] 开始...[/dim]")
    elif event_type == "node_end":
        node = data.get("node", "")
        info_parts = []
        for k, v in (data or {}).items():
            if k != "node":
                info_parts.append(f"{k}={v}")
        info = ", ".join(info_parts)
        console.print(f"[dim]── [{node}] 完成 {info}[/dim]")
    elif event_type == "loop_start":
        console.print(f"\n[bold]━━ 排查迭代 {data['iteration']} ━━[/bold]")
    elif event_type == "loop_continue":
        console.print(f"[yellow]↺ 证据不足，继续排查...[/yellow]")
    elif event_type == "plan_detail":
        console.print("[dim]── planner 计划明细[/dim]")
        for i, item in enumerate(data.get("plan", []), 1):
            tool = item.get("tool", "")
            args = item.get("args", {})
            args_str = str(args)
            if len(args_str) > 500:
                args_str = args_str[:500] + "..."
            console.print(f"  [cyan]{i}. {tool}[/cyan]")
            # console.print(f"     [dim]{args_str}[/dim]")
    elif event_type == "tool_call":
        name = data['name']
        extra = ""
        if name == "sls_search_errors":
            args = data.get("input") or {}
            if isinstance(args, dict) and not args.get("keyword"):
                extra = " [dim]（全量错误扫描）[/dim]"
        console.print(f"  [cyan]→ {name}[/cyan]{extra}")
        if data.get("input"):
            input_str = str(data["input"])
            if len(input_str) > 200:
                input_str = input_str[:200] + "..."
            console.print(f"    [dim]{input_str}[/dim]")
    elif event_type == "tool_result":
        flags = []
        if data.get("content_truncated"):
            flags.append("[裁短]")
        if data.get("hit_log_limit"):
            flags.append("[触及上限]")
        flag_str = " " + " ".join(flags) if flags else ""
        console.print(
            f"    [green]✓ {data['result_len']} 字节, "
            f"{data.get('latency_ms', 0)}ms{flag_str}[/green]"
        )
    elif event_type == "tool_dedup":
        console.print(f"    [dim]⊘ 跳过重复: {data['name']}[/dim]")
    elif event_type == "tool_limit":
        if "max_query" in data:
            console.print(
                f"[yellow]⚠ 已达工具调用上限 "
                f"(本问题 {data.get('query_tool_calls')}/{data.get('max_query')}, "
                f"state {data.get('total_tool_calls')}/{data.get('max_total')})[/yellow]"
            )
        else:
            console.print(f"[yellow]⚠ 已达工具调用上限 ({data['max']})[/yellow]")
    elif event_type == "no_plan":
        console.print("[dim]── planner 无计划，进入报告生成[/dim]")
    elif event_type == "skill_reroute":
        console.print(f"[cyan]⟳ 证据路由命中 Skill：{data['skills']}[/cyan]")
    elif event_type == "evidence_compress":
        console.print(
            f"[dim]Evidence 已压缩：新增 {data['compressed']} 条摘要，保留最近 {data['keep_recent_turns']} 轮原始证据[/dim]"
        )
    elif event_type == "history_compress":
        console.print(f"[dim]History 已压缩：{data['before']} -> {data['after']} 条[/dim]")
    # elif event_type == "rag_history_index": # 废除
        # if data.get("status") == "indexed":
        #     console.print(f"[magenta]RAG 历史库已保存当前案例：{data.get('chunk_id')}[/magenta]")
        # elif data.get("reason") == "near_duplicate":
        #     score = data.get("score")
        #     score_text = f"{score:.4f}" if score is not None else "None"
        #     console.print(f"[dim]RAG 历史库跳过保存：近似重复，score={score_text}[/dim]")
        # else:
        #     console.print(f"[dim]RAG 历史库跳过保存：{data.get('reason')}[/dim]")
    elif event_type == "rag_hit":
        label = {"history": "历史案例", "skill": "Skill"}.get(data["type"], data["type"])
        extra = ""
        if data.get("type") == "skill":
            top_score = data.get("top_score")
            top_score_text = f"{top_score:.4f}" if top_score is not None else "None"
            reason = f"，reason={data.get('reason')}" if data.get("reason") else ""
            extra = f"，top_score={top_score_text}，threshold={data.get('threshold'):.2f}，matched={data.get('matched_skills')}{reason}"
        console.print(f"[magenta]📚 RAG 命中 {label}：{data['count']} 条{extra}[/magenta]")
    elif event_type == "skill_inherit":
        console.print(f"[cyan]⟳ 继承前轮 Skill：{data['skills']}（{data['reason']}）[/cyan]")
    elif event_type == "skill_inherit_skip":
        console.print(
            f"[dim]⊘ 跳过 Skill 继承：{data['reason']} "
            f"prev={data.get('previous_trace_id')} current={data.get('current_trace_id')}[/dim]"
        )
    elif event_type == "segment_archive":
        archived = data.get("archived") or []
        reason = data.get("reason") or data.get("transition_type") or "unknown"
        archived_trace_ids = data.get("archived_trace_ids") or []
        new_trace_ids = data.get("new_trace_ids") or []
        transition_type = data.get("transition_type") or "unknown"

        console.print(
            f"[dim]── Segment 归档：原因={escape(str(reason))}，类型={escape(str(transition_type))}[/dim]"
        )
        console.print(
            f"[dim]   归档 trace：{escape(_format_trace_ids(archived_trace_ids))}[/dim]"
        )
        if new_trace_ids:
            console.print(
                f"[dim]   新 trace：{escape(_format_trace_ids(new_trace_ids))}[/dim]"
            )
        for idx, segment in enumerate(archived, 1):
            segment_trace_ids = _segment_value(segment, "trace_ids") or (
                [_segment_value(segment, "trace_id")] if _segment_value(segment, "trace_id") else []
            )
            conclusion = _shorten_text(_segment_value(segment, "conclusion"), 180)
            root_cause = _segment_value(segment, "root_cause") or "unknown"
            console.print(
                f"[dim]   {idx}. trace={escape(_format_trace_ids(segment_trace_ids, limit=6))}；"
                f"根因={escape(str(root_cause))}；结论={escape(conclusion)}[/dim]"
            )


@click.group()
def main():
    """TokenHub 网关运维 Agent"""


@main.command()
@click.argument("query")
@click.option("--mode", type=click.Choice(["graph", "react"]), default="graph",
              help="运行模式: graph (Phase 2) 或 react (Phase 1 fallback)")
def ask(query: str, mode: str):
    """运行单次故障排查。\n\nQUERY: 自然语言问题"""
    console.print(Panel(query, title="[bold]问题[/bold]", expand=False))
    console.print(f"[dim]模式: {mode}[/dim]\n")

    if mode == "react":
        from agent.react import run_investigation
        report = asyncio.run(run_investigation(query, on_event=_handle_event_react))
    else:
        from agent.graph import run_graph
        report = asyncio.run(run_graph(query, on_event=_handle_event_graph))

    console.print()
    console.print(Markdown(report))


@main.command()
@click.option("--force", is_flag=True, help="强制重新索引（清除旧数据后重新导入）")
def index(force: bool):
    """将 Skill 文档索引到 RAG 向量库。"""
    from agent.rag import get_store
    from agent.rag.indexer import index_skills

    store = get_store()

    existing = store.count({"source_type": "skill"})
    if existing > 0 and not force:
        console.print(f"[dim]RAG 中已有 {existing} 条 skill chunks，跳过。使用 --force 强制重建。[/dim]")
    else:
        count = index_skills(store)
        console.print(f"[green]OK 已索引 {count} 条 skill chunks[/green]")


@main.command()
def chat():
    """多轮对话模式（推荐）。支持跨轮上下文累积。"""
    from agent.session import SessionManager

    session_mgr = SessionManager()
    session = session_mgr.new_session()
    _run_chat_loop(session)


@main.command()
@click.argument("session_id")
def resume(session_id: str):
    """恢复历史 chat session，并继续多轮排查。"""
    from agent.persistence import init_db, load_latest_session_snapshot, load_session_snapshots

    init_db()
    latest = load_latest_session_snapshot(session_id)
    if not latest:
        console.print(f"[yellow]未找到 session: {session_id}[/yellow]")
        return

    snapshots = load_session_snapshots(session_id)
    restored = _restore_session_from_snapshot(latest)
    _render_resume_history(snapshots)
    _render_resume_memory(restored)
    _run_chat_loop(restored, resumed=True)


@main.command()
@click.option("--mode", type=click.Choice(["graph", "react"]), default="graph",
              help="运行模式: graph (Phase 2) 或 react (Phase 1 fallback)")
def repl(mode: str):
    """[已废弃] 请使用 chat 命令。单轮交互式 REPL 模式。"""
    console.print("[yellow]repl 命令已废弃，请使用 tokenhub-agent chat 进入多轮对话模式。[/yellow]\n")
    console.print(f"[bold green]TokenHub Agent REPL[/bold green]  (模式: {mode}, 输入 exit 退出)")

    handler = _handle_event_graph if mode == "graph" else _handle_event_react

    while True:
        try:
            query = console.input("\n[bold]>[/bold] ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not query or query.lower() in ("exit", "quit"):
            break

        if mode == "react":
            from agent.react import run_investigation
            report = asyncio.run(run_investigation(query, on_event=handler))
        else:
            from agent.graph import run_graph
            report = asyncio.run(run_graph(query, on_event=handler))

        console.print()
        console.print(Markdown(report))

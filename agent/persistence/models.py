"""Phase 2 persistence: SQLAlchemy ORM models.

Schema (fresh for P2, not compatible with P1):
- threads: 每次调查会话
- messages: 用户/助手消息
- tool_calls: MCP 工具调用审计
- evidences: planner→executor 累积的证据
- reports: 最终生成的报告
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from agent.config import AGENT_DB_PATH


class Base(DeclarativeBase):
    pass


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_query: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="running")  # running/done/failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    skills_hit: Mapped[list | None] = mapped_column(JSON, nullable=True)
    iterations: Mapped[int] = mapped_column(Integer, default=0)

    messages: Mapped[list["Message"]] = relationship(back_populates="thread")
    tool_calls: Mapped[list["ToolCallRecord"]] = relationship(back_populates="thread")
    reports: Mapped[list["Report"]] = relationship(back_populates="thread")
    snapshots: Mapped[list["SessionSnapshot"]] = relationship(back_populates="thread")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), ForeignKey("threads.id"))
    role: Mapped[str] = mapped_column(String(16))  # user/assistant/system/tool
    node: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 哪个 LangGraph 节点
    content: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    thread: Mapped[Thread] = relationship(back_populates="messages")


class ToolCallRecord(Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), ForeignKey("threads.id"))
    tool: Mapped[str] = mapped_column(String(64))
    args_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    args_hash: Mapped[str] = mapped_column(String(32), index=True)  # md5(tool+args) 用于去重
    result_size: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    thread: Mapped[Thread] = relationship(back_populates="tool_calls")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), ForeignKey("threads.id"))
    conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    recommendations: Mapped[str | None] = mapped_column(Text, nullable=True)
    skills_hit: Mapped[list | None] = mapped_column(JSON, nullable=True)
    full_report: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    thread: Mapped[Thread] = relationship(back_populates="reports")


class SessionSnapshot(Base):
    __tablename__ = "session_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    thread_id: Mapped[str] = mapped_column(String(64), ForeignKey("threads.id"))
    turn_index: Mapped[int] = mapped_column(Integer)
    facts_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evidence_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    skills: Mapped[list | None] = mapped_column(JSON, nullable=True)
    report: Mapped[str | None] = mapped_column(Text, nullable=True)
    global_plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_history_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    accumulated_evidence_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    session_memory_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    thread: Mapped[Thread] = relationship(back_populates="snapshots")


# Engine + session factory (lazy-initialized on first use)
_engine = None
_SessionLocal = None


def init_db() -> None:
    """Create tables if they don't exist."""
    global _engine, _SessionLocal
    from pathlib import Path
    Path(AGENT_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{AGENT_DB_PATH}", future=True)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(_engine)


def session():
    """Return a new SQLAlchemy session (caller must close)."""
    if _SessionLocal is None:
        init_db()
    return _SessionLocal()

"""SQLAlchemy database models for the Telegram Orchestrator."""

from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text, ForeignKey,
    JSON, Enum as SQLEnum, Index, UniqueConstraint
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


class TaskStatus(str, Enum):
    """Task execution status."""
    PENDING = "pending"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(str, Enum):
    """Task priority levels."""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class AgentStatus(str, Enum):
    """Agent status."""
    IDLE = "idle"
    BUSY = "busy"
    PAUSED = "paused"
    ERROR = "error"
    OFFLINE = "offline"


class DecisionStatus(str, Enum):
    """Decision point status."""
    PENDING = "pending"
    ANSWERED = "answered"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class Task(Base):
    """Task model for tracking development tasks."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    full_context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(SQLEnum(TaskStatus), default=TaskStatus.PENDING)
    priority: Mapped[TaskPriority] = mapped_column(SQLEnum(TaskPriority), default=TaskPriority.NORMAL)

    # Assignment
    assigned_agent_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("agents.id"), nullable=True)

    # Parent task for subtasks
    parent_task_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=True)

    # Dependencies (stored as JSON list of task IDs)
    dependencies: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Git context
    branch_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    pr_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # Execution details
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Metrics
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Telegram context
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    assigned_agent: Mapped[Optional["Agent"]] = relationship("Agent", back_populates="tasks")
    parent_task: Mapped[Optional["Task"]] = relationship("Task", remote_side=[id], back_populates="subtasks")
    subtasks: Mapped[list["Task"]] = relationship("Task", back_populates="parent_task")
    decisions: Mapped[list["Decision"]] = relationship("Decision", back_populates="task")
    activity_logs: Mapped[list["ActivityLog"]] = relationship("ActivityLog", back_populates="task")

    __table_args__ = (
        Index("idx_task_status", "status"),
        Index("idx_task_priority", "priority"),
        Index("idx_task_chat_id", "chat_id"),
    )


class Agent(Base):
    """Agent model for tracking Claude Code agent instances."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    status: Mapped[AgentStatus] = mapped_column(SQLEnum(AgentStatus), default=AgentStatus.IDLE)

    # Workspace
    workspace_path: Mapped[str] = mapped_column(String(512), nullable=False)
    current_branch: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Current task
    current_task_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Process info
    process_pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Metrics
    tasks_completed: Mapped[int] = mapped_column(Integer, default=0)
    tasks_failed: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Health
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    tasks: Mapped[list["Task"]] = relationship("Task", back_populates="assigned_agent")
    activity_logs: Mapped[list["ActivityLog"]] = relationship("ActivityLog", back_populates="agent")

    __table_args__ = (
        Index("idx_agent_status", "status"),
    )


class Decision(Base):
    """Decision model for tracking human escalation points."""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)

    # Question details
    question: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    options: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # List of options
    default_option: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Response
    status: Mapped[DecisionStatus] = mapped_column(SQLEnum(DecisionStatus), default=DecisionStatus.PENDING)
    response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    responded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Telegram
    message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Timeout
    timeout_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    task: Mapped["Task"] = relationship("Task", back_populates="decisions")

    __table_args__ = (
        Index("idx_decision_status", "status"),
        Index("idx_decision_task", "task_id"),
    )


class ActivityLog(Base):
    """Activity log for tracking all system events."""

    __tablename__ = "activity_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Context
    agent_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("agents.id"), nullable=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=True)

    # Event details
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    level: Mapped[str] = mapped_column(String(20), default="info")  # info, warning, error

    # Timestamp
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    agent: Mapped[Optional["Agent"]] = relationship("Agent", back_populates="activity_logs")
    task: Mapped[Optional["Task"]] = relationship("Task", back_populates="activity_logs")

    __table_args__ = (
        Index("idx_activity_timestamp", "timestamp"),
        Index("idx_activity_action", "action"),
    )


class DailyStats(Base):
    """Daily statistics for reporting."""

    __tablename__ = "daily_stats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Task metrics
    tasks_created: Mapped[int] = mapped_column(Integer, default=0)
    tasks_completed: Mapped[int] = mapped_column(Integer, default=0)
    tasks_failed: Mapped[int] = mapped_column(Integer, default=0)

    # Cost metrics
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Decision metrics
    decisions_requested: Mapped[int] = mapped_column(Integer, default=0)
    decisions_answered: Mapped[int] = mapped_column(Integer, default=0)
    avg_decision_time_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    # PR metrics
    prs_created: Mapped[int] = mapped_column(Integer, default=0)
    prs_merged: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("date", name="uq_daily_stats_date"),
    )


class FileLock(Base):
    """File lock for preventing conflicts between agents."""

    __tablename__ = "file_locks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id"), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)

    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("file_path", name="uq_file_lock_path"),
        Index("idx_file_lock_agent", "agent_id"),
    )


# Database engine and session
_engine = None
_async_session_maker = None


async def init_database(database_url: str):
    """Initialize the database connection."""
    global _engine, _async_session_maker

    _engine = create_async_engine(database_url, echo=False)
    _async_session_maker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    """Get a database session."""
    if _async_session_maker is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    async with _async_session_maker() as session:
        yield session


def get_session_maker() -> async_sessionmaker:
    """Get the session maker."""
    if _async_session_maker is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _async_session_maker

"""Pydantic schemas for request/response validation."""

from datetime import datetime
from typing import Optional
from enum import Enum

from pydantic import BaseModel, Field


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


# Task Schemas
class TaskCreate(BaseModel):
    """Schema for creating a new task."""
    description: str = Field(..., min_length=1, max_length=10000)
    full_context: Optional[str] = None
    priority: TaskPriority = TaskPriority.NORMAL
    parent_task_id: Optional[str] = None
    dependencies: Optional[list[str]] = None
    chat_id: int
    message_id: Optional[int] = None


class TaskUpdate(BaseModel):
    """Schema for updating a task."""
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    priority: Optional[TaskPriority] = None
    assigned_agent_id: Optional[str] = None
    branch_name: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    error_message: Optional[str] = None
    tokens_used: Optional[int] = None
    estimated_cost_usd: Optional[float] = None


class TaskResponse(BaseModel):
    """Schema for task response."""
    id: str
    description: str
    status: TaskStatus
    priority: TaskPriority
    assigned_agent_id: Optional[str]
    parent_task_id: Optional[str]
    dependencies: Optional[list[str]]
    branch_name: Optional[str]
    pr_number: Optional[int]
    pr_url: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    error_message: Optional[str]
    tokens_used: int
    estimated_cost_usd: float
    chat_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TaskSummary(BaseModel):
    """Brief task summary for status updates."""
    id: str
    description: str
    status: TaskStatus
    priority: TaskPriority
    assigned_agent: Optional[str] = None
    progress_percent: int = 0


# Agent Schemas
class AgentCreate(BaseModel):
    """Schema for creating an agent."""
    name: str = Field(..., min_length=1, max_length=100)
    workspace_path: str


class AgentUpdate(BaseModel):
    """Schema for updating an agent."""
    status: Optional[AgentStatus] = None
    current_task_id: Optional[str] = None
    current_branch: Optional[str] = None
    process_pid: Optional[int] = None
    error_count: Optional[int] = None
    last_error: Optional[str] = None


class AgentResponse(BaseModel):
    """Schema for agent response."""
    id: str
    name: str
    status: AgentStatus
    workspace_path: str
    current_branch: Optional[str]
    current_task_id: Optional[str]
    tasks_completed: int
    tasks_failed: int
    total_tokens_used: int
    total_cost_usd: float
    last_heartbeat: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class AgentSummary(BaseModel):
    """Brief agent summary for status."""
    id: str
    name: str
    status: AgentStatus
    current_task: Optional[str] = None


# Decision Schemas
class DecisionCreate(BaseModel):
    """Schema for creating a decision request."""
    task_id: str
    question: str
    context: Optional[str] = None
    options: Optional[list[str]] = None
    default_option: Optional[str] = None
    timeout_seconds: int = 3600


class DecisionResponse(BaseModel):
    """Schema for responding to a decision."""
    response: str


class DecisionDetail(BaseModel):
    """Full decision detail."""
    id: str
    task_id: str
    question: str
    context: Optional[str]
    options: Optional[list[str]]
    default_option: Optional[str]
    status: DecisionStatus
    response: Optional[str]
    responded_at: Optional[datetime]
    timeout_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


# Status Schemas
class SystemStatus(BaseModel):
    """Overall system status."""
    total_agents: int
    active_agents: int
    idle_agents: int
    total_tasks: int
    pending_tasks: int
    in_progress_tasks: int
    completed_tasks_today: int
    failed_tasks_today: int
    pending_decisions: int
    daily_cost_usd: float
    daily_budget_usd: float


class DailyDigest(BaseModel):
    """Daily summary digest."""
    date: datetime
    tasks_completed: int
    tasks_failed: int
    prs_created: int
    prs_merged: int
    total_tokens: int
    total_cost_usd: float
    decisions_made: int
    top_errors: list[str]
    agent_performance: list[AgentSummary]


# Notification Schemas
class Notification(BaseModel):
    """Notification to send to Telegram."""
    type: str  # task_start, task_complete, error, pr_ready, decision_needed
    title: str
    message: str
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    actions: Optional[list[str]] = None  # Inline keyboard options


# Command Schemas
class CreateTaskCommand(BaseModel):
    """Command to create a new task."""
    description: str
    priority: TaskPriority = TaskPriority.NORMAL
    voice_transcription: Optional[str] = None


class SetModeCommand(BaseModel):
    """Command to set autonomy mode."""
    mode: str  # full-auto, balanced, supervised


class SetBudgetCommand(BaseModel):
    """Command to set daily budget."""
    budget_usd: float

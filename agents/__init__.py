"""Agents package for Claude Code wrapper and management."""

from .claude_wrapper import ClaudeCodeWrapper, ClaudeCodeProcess, AgentPool
from .workspace import WorkspaceManager, generate_branch_name
from .monitor import AgentMonitor, AgentActivity, ActivityType, ProgressReport

__all__ = [
    "ClaudeCodeWrapper",
    "ClaudeCodeProcess",
    "AgentPool",
    "WorkspaceManager",
    "generate_branch_name",
    "AgentMonitor",
    "AgentActivity",
    "ActivityType",
    "ProgressReport",
]

"""Progress monitoring for Claude Code agents."""

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum

from git import Repo
import structlog

logger = structlog.get_logger()


class ActivityType(str, Enum):
    """Types of agent activity."""
    THINKING = "thinking"
    READING = "reading"
    WRITING = "writing"
    EDITING = "editing"
    RUNNING = "running"
    SEARCHING = "searching"
    COMMITTING = "committing"
    IDLE = "idle"
    ERROR = "error"


@dataclass
class AgentActivity:
    """Represents an agent's current activity."""
    type: ActivityType
    description: str
    file_path: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    details: Optional[dict] = None


@dataclass
class ProgressReport:
    """Progress report for a task."""
    task_id: str
    agent_id: str
    status: str
    current_activity: Optional[AgentActivity]
    files_modified: list[str]
    files_created: list[str]
    commits_made: int
    lines_changed: int
    duration_seconds: float
    estimated_completion: Optional[float]  # 0.0 to 1.0
    last_output: str


class AgentMonitor:
    """Monitors Claude Code agent progress."""

    def __init__(
        self,
        agent_id: str,
        workspace_path: Path,
        on_activity: Optional[Callable[[AgentActivity], Any]] = None,
        on_progress: Optional[Callable[[ProgressReport], Any]] = None
    ):
        self.agent_id = agent_id
        self.workspace_path = Path(workspace_path)
        self.on_activity = on_activity
        self.on_progress = on_progress

        self.current_activity: Optional[AgentActivity] = None
        self.activities: list[AgentActivity] = []
        self.task_id: Optional[str] = None
        self.started_at: Optional[datetime] = None

        self._monitoring = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._last_file_state: dict[str, float] = {}
        self._output_buffer: list[str] = []

    def start(self, task_id: str):
        """Start monitoring."""
        self.task_id = task_id
        self.started_at = datetime.utcnow()
        self._monitoring = True
        self.activities = []
        self._output_buffer = []
        self._capture_file_state()

        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Agent monitoring started", agent_id=self.agent_id, task_id=task_id)

    async def stop(self):
        """Stop monitoring."""
        self._monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Agent monitoring stopped", agent_id=self.agent_id)

    def process_output(self, line: str):
        """Process a line of output from the agent."""
        self._output_buffer.append(line)

        # Keep buffer manageable
        if len(self._output_buffer) > 1000:
            self._output_buffer = self._output_buffer[-500:]

        # Detect activity from output
        activity = self._detect_activity(line)
        if activity:
            self._update_activity(activity)

    def _detect_activity(self, line: str) -> Optional[AgentActivity]:
        """Detect the type of activity from output line."""
        line_lower = line.lower()

        # Reading files
        if "reading" in line_lower or "read file" in line_lower:
            file_match = re.search(r"(?:reading|read file[s]?:?)\s*[`'\"]?([^\s`'\"]+)", line, re.IGNORECASE)
            return AgentActivity(
                type=ActivityType.READING,
                description=f"Reading {file_match.group(1) if file_match else 'file'}",
                file_path=file_match.group(1) if file_match else None
            )

        # Writing files
        if "writing" in line_lower or "write file" in line_lower or "creating" in line_lower:
            file_match = re.search(r"(?:writing|write|creating)[^`'\"]*[`'\"]?([^\s`'\"]+)", line, re.IGNORECASE)
            return AgentActivity(
                type=ActivityType.WRITING,
                description=f"Writing {file_match.group(1) if file_match else 'file'}",
                file_path=file_match.group(1) if file_match else None
            )

        # Editing files
        if "editing" in line_lower or "modifying" in line_lower or "updating" in line_lower:
            file_match = re.search(r"(?:editing|modifying|updating)[^`'\"]*[`'\"]?([^\s`'\"]+)", line, re.IGNORECASE)
            return AgentActivity(
                type=ActivityType.EDITING,
                description=f"Editing {file_match.group(1) if file_match else 'file'}",
                file_path=file_match.group(1) if file_match else None
            )

        # Running commands
        if "running" in line_lower or "executing" in line_lower or "$ " in line:
            cmd_match = re.search(r"(?:running|executing|[$])\s*(.+)", line, re.IGNORECASE)
            return AgentActivity(
                type=ActivityType.RUNNING,
                description=f"Running command",
                details={"command": cmd_match.group(1)[:100] if cmd_match else ""}
            )

        # Searching
        if "searching" in line_lower or "looking for" in line_lower or "finding" in line_lower:
            return AgentActivity(
                type=ActivityType.SEARCHING,
                description="Searching codebase"
            )

        # Thinking/Planning
        if "thinking" in line_lower or "planning" in line_lower or "analyzing" in line_lower:
            return AgentActivity(
                type=ActivityType.THINKING,
                description="Analyzing and planning"
            )

        # Git operations
        if "commit" in line_lower or "git" in line_lower:
            return AgentActivity(
                type=ActivityType.COMMITTING,
                description="Git operation"
            )

        # Error detection
        if "error" in line_lower or "failed" in line_lower or "exception" in line_lower:
            return AgentActivity(
                type=ActivityType.ERROR,
                description=line[:100]
            )

        return None

    def _update_activity(self, activity: AgentActivity):
        """Update the current activity."""
        self.current_activity = activity
        self.activities.append(activity)

        # Notify callback
        if self.on_activity:
            try:
                result = self.on_activity(activity)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:
                logger.error("Activity callback error", error=str(e))

    async def _monitor_loop(self):
        """Background monitoring loop."""
        while self._monitoring:
            try:
                await self._check_file_changes()
                await self._emit_progress()
                await asyncio.sleep(5)  # Check every 5 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Monitor loop error", error=str(e))

    def _capture_file_state(self):
        """Capture current file modification times."""
        self._last_file_state = {}
        try:
            for path in self.workspace_path.rglob("*"):
                if path.is_file() and not self._is_ignored(path):
                    self._last_file_state[str(path)] = path.stat().st_mtime
        except Exception as e:
            logger.error("Failed to capture file state", error=str(e))

    def _is_ignored(self, path: Path) -> bool:
        """Check if a path should be ignored."""
        ignore_patterns = [".git", "__pycache__", "node_modules", ".venv", "venv", ".env"]
        return any(p in str(path) for p in ignore_patterns)

    async def _check_file_changes(self):
        """Check for file changes in workspace."""
        try:
            current_state = {}
            for path in self.workspace_path.rglob("*"):
                if path.is_file() and not self._is_ignored(path):
                    current_state[str(path)] = path.stat().st_mtime

            # Detect new/modified files
            for path, mtime in current_state.items():
                if path not in self._last_file_state:
                    # New file
                    activity = AgentActivity(
                        type=ActivityType.WRITING,
                        description=f"Created {Path(path).name}",
                        file_path=path
                    )
                    self._update_activity(activity)
                elif mtime > self._last_file_state[path]:
                    # Modified file
                    activity = AgentActivity(
                        type=ActivityType.EDITING,
                        description=f"Modified {Path(path).name}",
                        file_path=path
                    )
                    self._update_activity(activity)

            self._last_file_state = current_state

        except Exception as e:
            logger.error("File change detection error", error=str(e))

    async def _emit_progress(self):
        """Emit a progress report."""
        if not self.on_progress or not self.task_id:
            return

        try:
            report = await self.get_progress_report()
            result = self.on_progress(report)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.error("Progress emission error", error=str(e))

    async def get_progress_report(self) -> ProgressReport:
        """Get current progress report."""
        files_modified = []
        files_created = []
        commits_made = 0
        lines_changed = 0

        try:
            repo = Repo(self.workspace_path)

            # Get changes
            diff = repo.index.diff(None)
            files_modified = [item.a_path for item in diff]

            # Untracked files are created
            files_created = repo.untracked_files

            # Count commits on current branch
            base_branch = "main"
            try:
                commits_made = len(list(repo.iter_commits(f"{base_branch}..HEAD")))
            except Exception:
                commits_made = 0

            # Count lines changed
            try:
                diff_stats = repo.git.diff("--stat")
                match = re.search(r"(\d+) insertion", diff_stats)
                if match:
                    lines_changed += int(match.group(1))
                match = re.search(r"(\d+) deletion", diff_stats)
                if match:
                    lines_changed += int(match.group(1))
            except Exception:
                pass

        except Exception as e:
            logger.error("Failed to get git stats", error=str(e))

        duration = 0.0
        if self.started_at:
            duration = (datetime.utcnow() - self.started_at).total_seconds()

        return ProgressReport(
            task_id=self.task_id or "",
            agent_id=self.agent_id,
            status="running" if self._monitoring else "stopped",
            current_activity=self.current_activity,
            files_modified=files_modified,
            files_created=files_created,
            commits_made=commits_made,
            lines_changed=lines_changed,
            duration_seconds=duration,
            estimated_completion=None,  # Would need task complexity estimation
            last_output="\n".join(self._output_buffer[-10:])
        )

    def get_activity_summary(self) -> dict:
        """Get a summary of agent activities."""
        activity_counts = {}
        for activity in self.activities:
            activity_counts[activity.type.value] = activity_counts.get(activity.type.value, 0) + 1

        files_touched = set()
        for activity in self.activities:
            if activity.file_path:
                files_touched.add(activity.file_path)

        return {
            "total_activities": len(self.activities),
            "activity_counts": activity_counts,
            "files_touched": len(files_touched),
            "current_activity": self.current_activity.description if self.current_activity else "idle",
            "duration_seconds": (datetime.utcnow() - self.started_at).total_seconds() if self.started_at else 0
        }

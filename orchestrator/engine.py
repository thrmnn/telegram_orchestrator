"""Core orchestration engine - the brain of the system."""

import asyncio
from datetime import datetime, timedelta
from typing import Optional, Callable, Any
import uuid

from sqlalchemy import select, update, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from config import get_settings, AutonomyMode
from models import (
    Task, Agent, Decision, ActivityLog, DailyStats,
    TaskStatus, TaskPriority, AgentStatus, DecisionStatus,
    init_database, get_session_maker
)
from .task_queue import TaskQueue, InMemoryTaskQueue, QueuedTask
from .agent_manager import AgentManager, AgentInstance
from .decision_engine import DecisionEngine, EscalationType
from agents import ProgressReport, AgentActivity

logger = structlog.get_logger()


class OrchestrationEngine:
    """
    The core orchestration engine that coordinates all components.

    This is the brain of the system - it receives tasks from Telegram,
    distributes them to agents, handles decisions, and manages the
    overall workflow.
    """

    def __init__(
        self,
        on_task_started: Optional[Callable[[Task, Agent], Any]] = None,
        on_task_completed: Optional[Callable[[Task], Any]] = None,
        on_task_failed: Optional[Callable[[Task, str], Any]] = None,
        on_decision_needed: Optional[Callable[[Decision, Task], Any]] = None,
        on_pr_ready: Optional[Callable[[Task], Any]] = None
    ):
        self.settings = get_settings()

        # Callbacks
        self.on_task_started = on_task_started
        self.on_task_completed = on_task_completed
        self.on_task_failed = on_task_failed
        self.on_decision_needed = on_decision_needed
        self.on_pr_ready = on_pr_ready

        # Components (initialized in start())
        self.task_queue: Optional[TaskQueue] = None
        self.agent_manager: Optional[AgentManager] = None
        self.decision_engine: Optional[DecisionEngine] = None

        # State
        self._running = False
        self._dispatch_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._daily_cost = 0.0

    async def start(self):
        """Start the orchestration engine."""
        if self._running:
            return

        logger.info("Starting orchestration engine")

        try:
            # Initialize database
            logger.info("Initializing database")
            await init_database(self.settings.database_url)
            logger.info("Database initialized")

            # Initialize task queue
            try:
                logger.info("Connecting to Redis")
                self.task_queue = TaskQueue(self.settings.redis_url)
                await self.task_queue.connect()
                logger.info("Redis connected")
            except Exception as e:
                logger.warning("Redis not available, using in-memory queue", error=str(e))
                self.task_queue = InMemoryTaskQueue()
                await self.task_queue.connect()
                logger.info("In-memory queue initialized")

            # Initialize decision engine
            logger.info("Initializing decision engine")
            self.decision_engine = DecisionEngine(
                on_escalation=self._handle_escalation
            )
            logger.info("Decision engine initialized")

            # Initialize agent manager
            logger.info("Initializing agent manager")
            self.agent_manager = AgentManager(
                on_task_complete=self._handle_task_complete,
                on_task_error=self._handle_task_error,
                on_progress=self._handle_progress,
                on_activity=self._handle_activity
            )
            await self.agent_manager.initialize()
            logger.info("Agent manager initialized")

            # Start background tasks
            self._running = True
            logger.info("Starting dispatch loop")
            self._dispatch_task = asyncio.create_task(self._dispatch_loop())
            logger.info("Starting cleanup loop")
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

            logger.info("Orchestration engine started successfully")
        except Exception as e:
            logger.error("Failed to start orchestration engine", error=str(e), exc_info=True)
            raise

    async def stop(self):
        """Stop the orchestration engine."""
        if not self._running:
            return

        self._running = False

        # Cancel background tasks
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Shutdown components
        if self.agent_manager:
            await self.agent_manager.shutdown()

        if self.task_queue:
            await self.task_queue.disconnect()

        logger.info("Orchestration engine stopped")

    async def create_task(
        self,
        description: str,
        chat_id: int,
        message_id: Optional[int] = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        context: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        dependencies: Optional[list[str]] = None
    ) -> Task:
        """
        Create a new task and add it to the queue.

        Args:
            description: Task description
            chat_id: Telegram chat ID for notifications
            message_id: Optional message ID for threading
            priority: Task priority
            context: Additional context
            parent_task_id: Parent task for subtasks
            dependencies: List of task IDs this depends on

        Returns:
            Created Task object
        """
        session_maker = get_session_maker()
        async with session_maker() as session:
            task = Task(
                description=description,
                full_context=context,
                priority=priority,
                parent_task_id=parent_task_id,
                dependencies={"task_ids": dependencies} if dependencies else None,
                chat_id=chat_id,
                message_id=message_id,
                status=TaskStatus.QUEUED
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)

            # Add to queue
            queued_task = QueuedTask(
                task_id=task.id,
                priority=priority,
                created_at=datetime.utcnow().isoformat(),
                dependencies=dependencies or []
            )
            await self.task_queue.enqueue(queued_task)

            # Log activity
            log = ActivityLog(
                task_id=task.id,
                action="task_created",
                details={"description": description[:100], "priority": priority.value}
            )
            session.add(log)
            await session.commit()

            logger.info("Task created", task_id=task.id, priority=priority.value)
            return task

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a task."""
        # Remove from queue
        await self.task_queue.cancel(task_id)

        session_maker = get_session_maker()
        async with session_maker() as session:
            task = await session.get(Task, task_id)
            if not task:
                return False

            # If assigned to agent, terminate
            if task.assigned_agent_id:
                instance = self.agent_manager.agents.get(task.assigned_agent_id)
                if instance:
                    await instance.wrapper.terminate_current()
                    await self.agent_manager.release_agent(task.assigned_agent_id)

            task.status = TaskStatus.CANCELLED
            await session.commit()

            logger.info("Task cancelled", task_id=task_id)
            return True

    async def pause_task(self, task_id: str) -> bool:
        """Pause a task in progress."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            task = await session.get(Task, task_id)
            if not task:
                return False

            if task.status == TaskStatus.IN_PROGRESS and task.assigned_agent_id:
                await self.agent_manager.pause_agent(task.assigned_agent_id)

            task.status = TaskStatus.BLOCKED
            await session.commit()

            return True

    async def retry_task(self, task_id: str) -> bool:
        """Retry a failed task."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            task = await session.get(Task, task_id)
            if not task or task.status not in [TaskStatus.FAILED, TaskStatus.CANCELLED]:
                return False

            task.status = TaskStatus.QUEUED
            task.error_message = None
            task.assigned_agent_id = None
            await session.commit()

            # Re-add to queue
            queued_task = QueuedTask(
                task_id=task.id,
                priority=task.priority,
                created_at=datetime.utcnow().isoformat(),
                dependencies=task.dependencies.get("task_ids", []) if task.dependencies else []
            )
            await self.task_queue.enqueue(queued_task)

            return True

    async def pause_all_tasks(self) -> dict:
        """Pause all running tasks."""
        count = 0
        for agent_id in list(self.agent_manager.agents.keys()):
            if await self.agent_manager.pause_agent(agent_id):
                count += 1

        return {"paused_count": count}

    async def get_system_status(self) -> dict:
        """Get overall system status."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            # Agent counts
            agent_result = await session.execute(select(Agent))
            agents = list(agent_result.scalars().all())

            total_agents = len(agents)
            active_agents = sum(1 for a in agents if a.status == AgentStatus.BUSY)
            idle_agents = sum(1 for a in agents if a.status == AgentStatus.IDLE)

            # Task counts
            today = datetime.utcnow().date()
            today_start = datetime.combine(today, datetime.min.time())

            pending_count = await session.scalar(
                select(func.count(Task.id)).where(Task.status == TaskStatus.PENDING)
            )
            in_progress_count = await session.scalar(
                select(func.count(Task.id)).where(Task.status == TaskStatus.IN_PROGRESS)
            )
            completed_today = await session.scalar(
                select(func.count(Task.id)).where(
                    and_(
                        Task.status == TaskStatus.COMPLETED,
                        Task.completed_at >= today_start
                    )
                )
            )
            failed_today = await session.scalar(
                select(func.count(Task.id)).where(
                    and_(
                        Task.status == TaskStatus.FAILED,
                        Task.completed_at >= today_start
                    )
                )
            )

            # Decision counts
            pending_decisions = await session.scalar(
                select(func.count(Decision.id)).where(
                    Decision.status == DecisionStatus.PENDING
                )
            )

            # Queue status
            queue_status = await self.task_queue.get_queue_status()

            return {
                "total_agents": total_agents,
                "active_agents": active_agents,
                "idle_agents": idle_agents,
                "total_tasks": queue_status["queued"] + queue_status["processing"],
                "pending_tasks": pending_count or 0,
                "in_progress_tasks": in_progress_count or 0,
                "completed_today": completed_today or 0,
                "failed_today": failed_today or 0,
                "pending_decisions": pending_decisions or 0,
                "daily_cost": self._daily_cost,
                "queue_status": queue_status
            }

    async def get_agents(self) -> list[Agent]:
        """Get all agents."""
        return await self.agent_manager.get_agents()

    async def get_pending_prs(self) -> list[Task]:
        """Get tasks with pending PRs."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            result = await session.execute(
                select(Task).where(
                    and_(
                        Task.status == TaskStatus.COMPLETED,
                        Task.pr_url.isnot(None)
                    )
                )
            )
            return list(result.scalars().all())

    async def approve_pr(self, task_id: str) -> dict:
        """Approve and merge a PR."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            task = await session.get(Task, task_id)
            if not task or not task.pr_url:
                return {"success": False, "error": "Task or PR not found"}

            # In a real implementation, this would call GitHub API
            # For now, just log the action
            logger.info("PR approved", task_id=task_id, pr_url=task.pr_url)

            return {"success": True, "pr_url": task.pr_url}

    async def get_logs(
        self,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 50
    ) -> list[ActivityLog]:
        """Get activity logs."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            query = select(ActivityLog).order_by(ActivityLog.timestamp.desc()).limit(limit)

            if task_id:
                query = query.where(ActivityLog.task_id == task_id)
            if agent_id:
                query = query.where(ActivityLog.agent_id == agent_id)

            result = await session.execute(query)
            return list(result.scalars().all())

    async def respond_to_decision(
        self,
        decision_id: str,
        response: Optional[str] = None,
        option_index: Optional[int] = None
    ) -> bool:
        """Respond to a decision request."""
        return await self.decision_engine.respond(
            decision_id=decision_id,
            response=response or "",
            option_index=option_index
        )

    async def skip_decision(self, decision_id: str) -> bool:
        """Skip a decision."""
        return await self.decision_engine.skip(decision_id)

    async def set_autonomy_mode(self, mode: AutonomyMode):
        """Set the autonomy mode."""
        self.settings.autonomy_mode = mode
        logger.info("Autonomy mode changed", mode=mode.value)

    async def set_daily_budget(self, budget: float):
        """Set the daily budget."""
        self.settings.daily_budget_usd = budget
        logger.info("Daily budget changed", budget=budget)

    async def restart_agent(self, agent_id: str) -> bool:
        """Restart an agent."""
        return await self.agent_manager.restart_agent(agent_id)

    # Private methods

    async def _dispatch_loop(self):
        """Background loop for dispatching tasks to agents."""
        while self._running:
            try:
                await self._dispatch_tasks()
                await asyncio.sleep(2)  # Check every 2 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Dispatch loop error", error=str(e))
                await asyncio.sleep(5)

    async def _dispatch_tasks(self):
        """Dispatch queued tasks to available agents."""
        # Check budget
        if self._daily_cost >= self.settings.daily_budget_usd:
            return

        # Get available agent
        instance = await self.agent_manager.get_available_agent()
        if not instance:
            return

        # Store agent_id separately since db_agent may be detached
        agent_id = None
        session_maker = get_session_maker()

        # Get agent ID in a session
        async with session_maker() as session:
            agent_db = await session.get(Agent, list(self.agent_manager.agents.keys())[0])
            # Find the correct agent by checking which one is idle
            for aid in self.agent_manager.agents.keys():
                agent_check = await session.get(Agent, aid)
                if agent_check and agent_check.status == AgentStatus.IDLE:
                    agent_id = aid
                    break

        if not agent_id:
            return

        # Get next task
        queued_task = await self.task_queue.dequeue(agent_id)
        if not queued_task:
            return

        # Get full task and agent from database
        async with session_maker() as session:
            task = await session.get(Task, queued_task.task_id)
            if not task:
                logger.warning("Task not found in database", task_id=queued_task.task_id)
                return

            # Get agent from database in same session
            agent = await session.get(Agent, agent_id)
            if not agent:
                logger.warning("Agent not found in database", agent_id=agent_id)
                return

            # Refresh to load all attributes
            await session.refresh(task)
            await session.refresh(agent)

            # Expunge objects from session so they can be used outside the session context
            session.expunge(task)
            session.expunge(agent)

            # Assign to agent
            if await self.agent_manager.assign_task(agent_id, task):
                # Notify
                if self.on_task_started:
                    try:
                        result = self.on_task_started(task, agent)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.error("Task started callback error", error=str(e))

                # Execute in background
                asyncio.create_task(
                    self._execute_task_wrapper(agent_id)
                )

    async def _execute_task_wrapper(self, agent_id: str):
        """Wrapper for task execution with error handling."""
        try:
            result = await self.agent_manager.execute_task(agent_id)

            # Update cost tracking
            # Estimate based on typical token usage
            estimated_tokens = result.get("duration_seconds", 0) * 100  # Rough estimate
            estimated_cost = estimated_tokens * 0.00001  # Rough cost per token
            self._daily_cost += estimated_cost

        except Exception as e:
            logger.error("Task execution wrapper error", error=str(e), agent_id=agent_id)

    async def _handle_task_complete(self, task: Task, result: dict):
        """Handle task completion."""
        # Mark in queue
        await self.task_queue.complete(task.id, success=True)

        # Notify
        if self.on_task_completed:
            try:
                callback_result = self.on_task_completed(task)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
            except Exception as e:
                logger.error("Task completed callback error", error=str(e))

    async def _handle_task_error(self, task: Task, error: str):
        """Handle task error."""
        # Mark in queue
        await self.task_queue.complete(task.id, success=False)

        # Notify
        if self.on_task_failed:
            try:
                callback_result = self.on_task_failed(task, error)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
            except Exception as e:
                logger.error("Task failed callback error", error=str(e))

    async def _handle_escalation(self, decision: Decision, task: Task):
        """Handle decision escalation."""
        if self.on_decision_needed:
            try:
                result = self.on_decision_needed(decision, task)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error("Decision escalation callback error", error=str(e))

    async def _handle_progress(self, agent_id: str, report: ProgressReport):
        """Handle progress report from agent."""
        # Could update database or notify based on settings
        pass

    async def _handle_activity(self, agent_id: str, activity: AgentActivity):
        """Handle activity update from agent."""
        # Could log or notify based on settings
        pass

    async def _cleanup_loop(self):
        """Background loop for cleanup tasks."""
        while self._running:
            try:
                # Clean up expired decisions
                if self.decision_engine:
                    await self.decision_engine.cleanup_expired()

                # Reset daily cost at midnight
                now = datetime.utcnow()
                if now.hour == 0 and now.minute < 5:
                    self._daily_cost = 0.0

                await asyncio.sleep(300)  # Run every 5 minutes
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Cleanup loop error", error=str(e))

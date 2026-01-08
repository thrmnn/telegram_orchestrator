"""Agent pool management for orchestrating Claude Code instances."""

import asyncio
from datetime import datetime, timedelta
from typing import Optional, Callable, Any
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from config import get_settings
from models import (
    Agent, AgentStatus, Task, TaskStatus, ActivityLog,
    get_session_maker
)
from agents import (
    ClaudeCodeWrapper, WorkspaceManager, AgentMonitor,
    AgentActivity, ProgressReport, generate_branch_name
)

logger = structlog.get_logger()


@dataclass
class AgentInstance:
    """Runtime state of an agent."""
    agent_id: str
    wrapper: ClaudeCodeWrapper
    monitor: AgentMonitor
    workspace_manager: WorkspaceManager
    current_task: Optional[Task] = None
    last_heartbeat: datetime = None

    def __post_init__(self):
        self.last_heartbeat = datetime.utcnow()


class AgentManager:
    """Manages the pool of Claude Code agents."""

    def __init__(
        self,
        on_task_complete: Optional[Callable[[Task, dict], Any]] = None,
        on_task_error: Optional[Callable[[Task, str], Any]] = None,
        on_progress: Optional[Callable[[str, ProgressReport], Any]] = None,
        on_activity: Optional[Callable[[str, AgentActivity], Any]] = None
    ):
        self.on_task_complete = on_task_complete
        self.on_task_error = on_task_error
        self.on_progress = on_progress
        self.on_activity = on_activity

        self.settings = get_settings()
        self.agents: dict[str, AgentInstance] = {}
        self._initialized = False
        self._health_check_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """Initialize the agent pool."""
        if self._initialized:
            return

        session_maker = get_session_maker()
        async with session_maker() as session:
            # Create or update agents in database
            for i in range(self.settings.agent_count):
                agent_name = f"agent_{i+1}"
                workspace_path = self.settings.agent_workspace_base / agent_name

                # Check if agent exists
                result = await session.execute(
                    select(Agent).where(Agent.name == agent_name)
                )
                db_agent = result.scalar_one_or_none()

                if db_agent:
                    # Update existing
                    db_agent.status = AgentStatus.IDLE
                    db_agent.workspace_path = str(workspace_path)
                    db_agent.current_task_id = None
                else:
                    # Create new
                    db_agent = Agent(
                        name=agent_name,
                        workspace_path=str(workspace_path),
                        status=AgentStatus.IDLE
                    )
                    session.add(db_agent)

            await session.commit()

            # Now create runtime instances
            result = await session.execute(select(Agent))
            agents = result.scalars().all()

            for db_agent in agents:
                await self._create_agent_instance(db_agent)

        # Start health check task
        self._health_check_task = asyncio.create_task(self._health_check_loop())

        self._initialized = True
        logger.info("Agent manager initialized", agent_count=len(self.agents))

    async def _create_agent_instance(self, db_agent: Agent) -> AgentInstance:
        """Create runtime instance for an agent."""
        workspace_path = self.settings.agent_workspace_base / db_agent.name

        # Create workspace manager
        workspace_manager = WorkspaceManager(
            base_path=workspace_path,
            repo_url=f"https://github.com/{self.settings.github_repo}.git" if self.settings.github_repo else None
        )

        # Create wrapper
        wrapper = ClaudeCodeWrapper(
            agent_id=db_agent.id,
            workspace_path=workspace_path
        )

        # Create monitor
        def on_activity(activity: AgentActivity):
            if self.on_activity:
                return self.on_activity(db_agent.id, activity)

        def on_progress(report: ProgressReport):
            if self.on_progress:
                return self.on_progress(db_agent.id, report)

        monitor = AgentMonitor(
            agent_id=db_agent.id,
            workspace_path=workspace_path,
            on_activity=on_activity,
            on_progress=on_progress
        )

        instance = AgentInstance(
            agent_id=db_agent.id,
            wrapper=wrapper,
            monitor=monitor,
            workspace_manager=workspace_manager
        )

        self.agents[db_agent.id] = instance
        return instance

    async def shutdown(self):
        """Shutdown all agents."""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        # Terminate all running processes
        for instance in self.agents.values():
            await instance.wrapper.terminate_current()
            await instance.monitor.stop()

        self._initialized = False
        logger.info("Agent manager shutdown")

    async def get_available_agent(self) -> Optional[AgentInstance]:
        """Get an available agent for task assignment."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            for agent_id, instance in self.agents.items():
                # Get agent from database
                agent = await session.get(Agent, agent_id)
                if agent and agent.status == AgentStatus.IDLE:
                    return instance

        return None

    async def assign_task(
        self,
        agent_id: str,
        task: Task,
        branch_name: Optional[str] = None
    ) -> bool:
        """
        Assign a task to an agent.

        Args:
            agent_id: Agent to assign to
            task: Task to assign
            branch_name: Optional branch name (generated if not provided)

        Returns:
            Success boolean
        """
        instance = self.agents.get(agent_id)
        if not instance:
            logger.error("Agent not found", agent_id=agent_id)
            return False

        session_maker = get_session_maker()
        async with session_maker() as session:
            # Update agent status
            await session.execute(
                update(Agent)
                .where(Agent.id == agent_id)
                .values(
                    status=AgentStatus.BUSY,
                    current_task_id=task.id
                )
            )

            # Update task
            await session.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(
                    status=TaskStatus.IN_PROGRESS,
                    assigned_agent_id=agent_id,
                    started_at=datetime.utcnow(),
                    branch_name=branch_name or generate_branch_name(task.description)
                )
            )

            await session.commit()

            # Refresh task
            result = await session.execute(select(Task).where(Task.id == task.id))
            refreshed_task = result.scalar_one()
            # Expunge task so it can be used outside session
            session.expunge(refreshed_task)

        instance.current_task = refreshed_task

        # Log activity
        await self._log_activity(
            agent_id=agent_id,
            task_id=task.id,
            action="task_assigned",
            details={"description": task.description[:100]}
        )

        logger.info(
            "Task assigned",
            agent_id=agent_id,
            task_id=task.id,
            branch=refreshed_task.branch_name
        )

        return True

    async def execute_task(self, agent_id: str) -> dict:
        """
        Execute the currently assigned task.

        Returns:
            Execution result dict
        """
        instance = self.agents.get(agent_id)
        if not instance or not instance.current_task:
            return {"success": False, "error": "No task assigned"}

        task = instance.current_task

        try:
            # Prepare workspace
            await instance.workspace_manager.create_workspace(
                agent_id=instance.agent_id,
                branch_name=task.branch_name
            )

            # Start monitoring
            instance.monitor.start(task.id)

            # Build prompt
            prompt = self._build_task_prompt(task)

            # Execute
            result = await instance.wrapper.execute_task(
                task_id=task.id,
                prompt=prompt,
                context=task.full_context,
                output_callback=instance.monitor.process_output,
                timeout=self.settings.agent_timeout_seconds
            )

            # Stop monitoring
            await instance.monitor.stop()

            # Handle result
            if result["success"]:
                await self._handle_success(instance, task, result)
            else:
                await self._handle_failure(instance, task, result)

            return result

        except Exception as e:
            logger.error("Task execution error", error=str(e), task_id=task.id)
            await instance.monitor.stop()
            await self._handle_failure(instance, task, {"errors": str(e)})
            return {"success": False, "error": str(e)}

    def _build_task_prompt(self, task: Task) -> str:
        """Build the prompt for Claude Code."""
        prompt = f"""You are working on the following development task:

{task.description}

Please complete this task by:
1. Understanding the requirements
2. Exploring the codebase as needed
3. Implementing the necessary changes
4. Writing or updating tests if applicable
5. Committing your changes with a descriptive message

Work branch: {task.branch_name}

Important guidelines:
- Make incremental commits as you progress
- Write clean, maintainable code
- Follow existing code conventions
- If you encounter ambiguity or need clarification, state what assumptions you're making
"""
        return prompt

    async def _handle_success(
        self,
        instance: AgentInstance,
        task: Task,
        result: dict
    ):
        """Handle successful task completion."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            # Update task
            await session.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(
                    status=TaskStatus.COMPLETED,
                    completed_at=datetime.utcnow()
                )
            )

            # Update agent
            await session.execute(
                update(Agent)
                .where(Agent.id == instance.agent_id)
                .values(
                    status=AgentStatus.IDLE,
                    current_task_id=None,
                    tasks_completed=Agent.tasks_completed + 1
                )
            )

            await session.commit()

        instance.current_task = None

        # Log
        await self._log_activity(
            agent_id=instance.agent_id,
            task_id=task.id,
            action="task_completed",
            details={"duration": result.get("duration_seconds")}
        )

        # Callback
        if self.on_task_complete:
            try:
                callback_result = self.on_task_complete(task, result)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
            except Exception as e:
                logger.error("Task complete callback error", error=str(e))

    async def _handle_failure(
        self,
        instance: AgentInstance,
        task: Task,
        result: dict
    ):
        """Handle task failure."""
        error_message = result.get("errors", "Unknown error")

        session_maker = get_session_maker()
        async with session_maker() as session:
            # Update task
            await session.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(
                    status=TaskStatus.FAILED,
                    completed_at=datetime.utcnow(),
                    error_message=error_message[:2000]
                )
            )

            # Update agent
            await session.execute(
                update(Agent)
                .where(Agent.id == instance.agent_id)
                .values(
                    status=AgentStatus.IDLE,
                    current_task_id=None,
                    tasks_failed=Agent.tasks_failed + 1,
                    error_count=Agent.error_count + 1,
                    last_error=error_message[:500]
                )
            )

            await session.commit()

        instance.current_task = None

        # Log
        await self._log_activity(
            agent_id=instance.agent_id,
            task_id=task.id,
            action="task_failed",
            level="error",
            details={"error": error_message[:500]}
        )

        # Callback
        if self.on_task_error:
            try:
                callback_result = self.on_task_error(task, error_message)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
            except Exception as e:
                logger.error("Task error callback error", error=str(e))

    async def release_agent(self, agent_id: str):
        """Release an agent back to the pool."""
        instance = self.agents.get(agent_id)
        if not instance:
            return

        session_maker = get_session_maker()
        async with session_maker() as session:
            await session.execute(
                update(Agent)
                .where(Agent.id == agent_id)
                .values(
                    status=AgentStatus.IDLE,
                    current_task_id=None
                )
            )
            await session.commit()

        instance.current_task = None
        logger.info("Agent released", agent_id=agent_id)

    async def pause_agent(self, agent_id: str) -> bool:
        """Pause an agent."""
        instance = self.agents.get(agent_id)
        if not instance:
            return False

        # Terminate current process
        await instance.wrapper.terminate_current()
        await instance.monitor.stop()

        session_maker = get_session_maker()
        async with session_maker() as session:
            await session.execute(
                update(Agent)
                .where(Agent.id == agent_id)
                .values(status=AgentStatus.PAUSED)
            )
            await session.commit()

        return True

    async def resume_agent(self, agent_id: str) -> bool:
        """Resume a paused agent."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            result = await session.execute(
                select(Agent).where(Agent.id == agent_id)
            )
            agent = result.scalar_one_or_none()

            if not agent or agent.status != AgentStatus.PAUSED:
                return False

            await session.execute(
                update(Agent)
                .where(Agent.id == agent_id)
                .values(status=AgentStatus.IDLE)
            )
            await session.commit()

        return True

    async def restart_agent(self, agent_id: str) -> bool:
        """Restart an agent (terminate and reinitialize)."""
        instance = self.agents.get(agent_id)
        if not instance:
            return False

        # Terminate current
        await instance.wrapper.terminate_current()
        await instance.monitor.stop()

        # Reset status
        session_maker = get_session_maker()
        async with session_maker() as session:
            await session.execute(
                update(Agent)
                .where(Agent.id == agent_id)
                .values(
                    status=AgentStatus.IDLE,
                    current_task_id=None,
                    error_count=0,
                    last_error=None
                )
            )
            await session.commit()

        instance.current_task = None
        logger.info("Agent restarted", agent_id=agent_id)
        return True

    async def get_agents(self) -> list[Agent]:
        """Get all agents."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            result = await session.execute(select(Agent))
            return list(result.scalars().all())

    async def get_agent(self, agent_id: str) -> Optional[Agent]:
        """Get a specific agent."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            return await session.get(Agent, agent_id)

    async def _log_activity(
        self,
        action: str,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        level: str = "info",
        details: Optional[dict] = None
    ):
        """Log an activity."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            log = ActivityLog(
                agent_id=agent_id,
                task_id=task_id,
                action=action,
                level=level,
                details=details
            )
            session.add(log)
            await session.commit()

    async def _health_check_loop(self):
        """Background loop for agent health checks."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self._perform_health_check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Health check error", error=str(e))

    async def _perform_health_check(self):
        """Perform health check on all agents."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            for agent_id, instance in self.agents.items():
                # Update heartbeat
                instance.last_heartbeat = datetime.utcnow()

                await session.execute(
                    update(Agent)
                    .where(Agent.id == agent_id)
                    .values(last_heartbeat=instance.last_heartbeat)
                )

                # Check for stale busy agents
                agent = await session.get(Agent, agent_id)
                if agent and agent.status == AgentStatus.BUSY:
                    if not instance.wrapper.current_process:
                        # Agent marked busy but no process running
                        logger.warning(
                            "Stale busy agent detected",
                            agent_id=agent_id
                        )
                        await session.execute(
                            update(Agent)
                            .where(Agent.id == agent_id)
                            .values(status=AgentStatus.IDLE, current_task_id=None)
                        )

            await session.commit()

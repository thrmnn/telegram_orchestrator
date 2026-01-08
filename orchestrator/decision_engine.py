"""Decision escalation engine for human-in-the-loop workflows."""

import asyncio
import re
from datetime import datetime, timedelta
from typing import Optional, Callable, Any
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from config import get_settings, AutonomyMode
from models import Decision, DecisionStatus, Task, TaskStatus, get_session_maker

logger = structlog.get_logger()


class EscalationType(str, Enum):
    """Types of decisions that can be escalated."""
    ARCHITECTURAL = "architectural"
    SECURITY = "security"
    BREAKING_CHANGE = "breaking_change"
    EXTERNAL_SERVICE = "external_service"
    DATABASE_MIGRATION = "database_migration"
    AMBIGUOUS_REQUIREMENT = "ambiguous_requirement"
    ERROR_RECOVERY = "error_recovery"
    RESOURCE_INTENSIVE = "resource_intensive"
    GENERAL = "general"


@dataclass
class EscalationTrigger:
    """Defines when escalation should occur."""
    type: EscalationType
    keywords: list[str]
    patterns: list[str]
    always_escalate: bool = False  # Escalate regardless of autonomy mode


# Default escalation triggers
DEFAULT_TRIGGERS = [
    EscalationTrigger(
        type=EscalationType.SECURITY,
        keywords=["password", "secret", "api key", "token", "credential", "authentication", "authorization"],
        patterns=[r"(?i)security\s+(?:issue|concern|risk)", r"(?i)vulnerab"],
        always_escalate=True
    ),
    EscalationTrigger(
        type=EscalationType.BREAKING_CHANGE,
        keywords=["breaking change", "deprecate", "remove api", "schema change"],
        patterns=[r"(?i)break(?:ing)?\s+(?:change|compatibility)", r"(?i)backwards?\s+compat"],
        always_escalate=True
    ),
    EscalationTrigger(
        type=EscalationType.DATABASE_MIGRATION,
        keywords=["migration", "alter table", "drop column", "rename column"],
        patterns=[r"(?i)database\s+(?:migration|schema)", r"(?i)(?:add|drop|alter)\s+(?:table|column)"],
        always_escalate=True
    ),
    EscalationTrigger(
        type=EscalationType.ARCHITECTURAL,
        keywords=["architecture", "design pattern", "refactor", "restructure"],
        patterns=[r"(?i)(?:major|significant)\s+(?:change|refactor)"],
        always_escalate=False
    ),
    EscalationTrigger(
        type=EscalationType.EXTERNAL_SERVICE,
        keywords=["external api", "third party", "integration", "webhook"],
        patterns=[r"(?i)external\s+(?:service|api)", r"(?i)third[\s-]party"],
        always_escalate=False
    ),
    EscalationTrigger(
        type=EscalationType.AMBIGUOUS_REQUIREMENT,
        keywords=["unclear", "ambiguous", "multiple options", "not sure"],
        patterns=[r"(?i)(?:should|could)\s+(?:i|we)", r"(?i)which\s+(?:approach|method|way)"],
        always_escalate=False
    ),
    EscalationTrigger(
        type=EscalationType.ERROR_RECOVERY,
        keywords=["error", "failed", "exception", "crash"],
        patterns=[r"(?i)(?:fatal|critical)\s+error", r"(?i)cannot\s+(?:proceed|continue)"],
        always_escalate=False
    ),
    EscalationTrigger(
        type=EscalationType.RESOURCE_INTENSIVE,
        keywords=["expensive", "time consuming", "large scale", "batch"],
        patterns=[r"(?i)(?:will|might)\s+take\s+(?:long|a while)", r"(?i)resource[\s-]intensive"],
        always_escalate=False
    ),
]


class DecisionEngine:
    """Engine for detecting and managing decision escalations."""

    def __init__(
        self,
        on_escalation: Optional[Callable[[Decision, Task], Any]] = None,
        triggers: Optional[list[EscalationTrigger]] = None
    ):
        self.on_escalation = on_escalation
        self.triggers = triggers or DEFAULT_TRIGGERS
        self.settings = get_settings()
        self._pending_decisions: dict[str, asyncio.Event] = {}

    def should_escalate(
        self,
        content: str,
        escalation_type: Optional[EscalationType] = None
    ) -> tuple[bool, Optional[EscalationType]]:
        """
        Determine if content should trigger an escalation.

        Returns:
            Tuple of (should_escalate, escalation_type)
        """
        mode = self.settings.autonomy_mode

        # In supervised mode, always escalate
        if mode == AutonomyMode.SUPERVISED:
            return True, EscalationType.GENERAL

        # Check triggers
        for trigger in self.triggers:
            # Check if this type matches requested type
            if escalation_type and trigger.type != escalation_type:
                continue

            matched = False

            # Check keywords
            content_lower = content.lower()
            for keyword in trigger.keywords:
                if keyword in content_lower:
                    matched = True
                    break

            # Check patterns
            if not matched:
                for pattern in trigger.patterns:
                    if re.search(pattern, content):
                        matched = True
                        break

            if matched:
                # In full-auto mode, only escalate if always_escalate is True
                if mode == AutonomyMode.FULL_AUTO and not trigger.always_escalate:
                    continue
                return True, trigger.type

        return False, None

    async def create_decision(
        self,
        task_id: str,
        question: str,
        context: Optional[str] = None,
        options: Optional[list[str]] = None,
        default_option: Optional[str] = None,
        escalation_type: Optional[EscalationType] = None
    ) -> Optional[Decision]:
        """
        Create a decision request for human input.

        Args:
            task_id: The task requiring the decision
            question: The question to ask
            context: Additional context
            options: List of options (if applicable)
            default_option: Default option if timeout occurs
            escalation_type: Type of escalation

        Returns:
            The created Decision or None if escalation not needed
        """
        # Check if escalation is needed
        should_ask, detected_type = self.should_escalate(
            f"{question} {context or ''}",
            escalation_type
        )

        if not should_ask:
            logger.debug("Escalation not triggered", task_id=task_id)
            return None

        session_maker = get_session_maker()
        async with session_maker() as session:
            # Get the task
            task = await session.get(Task, task_id)
            if not task:
                logger.error("Task not found for decision", task_id=task_id)
                return None

            # Calculate timeout
            timeout_at = datetime.utcnow() + timedelta(
                seconds=self.settings.decision_timeout_seconds
            )

            # Create decision
            decision = Decision(
                task_id=task_id,
                question=question,
                context=context,
                options={"options": options} if options else None,
                default_option=default_option,
                status=DecisionStatus.PENDING,
                timeout_at=timeout_at
            )

            session.add(decision)

            # Update task status
            task.status = TaskStatus.BLOCKED

            await session.commit()
            await session.refresh(decision)

            logger.info(
                "Decision created",
                decision_id=decision.id,
                task_id=task_id,
                type=detected_type.value if detected_type else "general"
            )

            # Create event for waiting
            self._pending_decisions[decision.id] = asyncio.Event()

            # Notify via callback
            if self.on_escalation:
                try:
                    result = self.on_escalation(decision, task)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error("Escalation callback error", error=str(e))

            return decision

    async def wait_for_decision(
        self,
        decision_id: str,
        timeout: Optional[float] = None
    ) -> Optional[str]:
        """
        Wait for a decision response.

        Args:
            decision_id: The decision to wait for
            timeout: Override timeout in seconds

        Returns:
            The response or default option if timeout
        """
        if timeout is None:
            timeout = self.settings.decision_timeout_seconds

        event = self._pending_decisions.get(decision_id)
        if not event:
            # Decision doesn't exist or already resolved
            session_maker = get_session_maker()
            async with session_maker() as session:
                decision = await session.get(Decision, decision_id)
                if decision and decision.status == DecisionStatus.ANSWERED:
                    return decision.response
                return None

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.info("Decision timeout", decision_id=decision_id)
            return await self._handle_timeout(decision_id)

        # Get the response
        session_maker = get_session_maker()
        async with session_maker() as session:
            decision = await session.get(Decision, decision_id)
            return decision.response if decision else None

    async def respond(
        self,
        decision_id: str,
        response: str,
        option_index: Optional[int] = None
    ) -> bool:
        """
        Record a response to a decision.

        Args:
            decision_id: The decision to respond to
            response: The response text
            option_index: If responding with an option, its index

        Returns:
            Success boolean
        """
        session_maker = get_session_maker()
        async with session_maker() as session:
            decision = await session.get(Decision, decision_id)
            if not decision:
                return False

            if decision.status != DecisionStatus.PENDING:
                logger.warning("Decision already resolved", decision_id=decision_id)
                return False

            # If option index provided, get the option text
            if option_index is not None and decision.options:
                options = decision.options.get("options", [])
                if 0 <= option_index < len(options):
                    response = options[option_index]

            decision.response = response
            decision.status = DecisionStatus.ANSWERED
            decision.responded_at = datetime.utcnow()

            # Update task status
            task = await session.get(Task, decision.task_id)
            if task and task.status == TaskStatus.BLOCKED:
                task.status = TaskStatus.QUEUED

            await session.commit()

            logger.info("Decision responded", decision_id=decision_id, response=response[:50])

            # Signal waiting coroutines
            event = self._pending_decisions.pop(decision_id, None)
            if event:
                event.set()

            return True

    async def skip(self, decision_id: str) -> bool:
        """
        Skip a decision and use default behavior.

        Returns:
            Success boolean
        """
        session_maker = get_session_maker()
        async with session_maker() as session:
            decision = await session.get(Decision, decision_id)
            if not decision:
                return False

            decision.status = DecisionStatus.SKIPPED
            decision.responded_at = datetime.utcnow()

            # Update task
            task = await session.get(Task, decision.task_id)
            if task and task.status == TaskStatus.BLOCKED:
                task.status = TaskStatus.QUEUED

            await session.commit()

            # Signal waiting
            event = self._pending_decisions.pop(decision_id, None)
            if event:
                event.set()

            return True

    async def _handle_timeout(self, decision_id: str) -> Optional[str]:
        """Handle a decision timeout."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            decision = await session.get(Decision, decision_id)
            if not decision:
                return None

            decision.status = DecisionStatus.TIMEOUT
            decision.responded_at = datetime.utcnow()

            # Use default option if available
            response = decision.default_option

            # Update task
            task = await session.get(Task, decision.task_id)
            if task and task.status == TaskStatus.BLOCKED:
                task.status = TaskStatus.QUEUED

            await session.commit()

            # Clean up
            self._pending_decisions.pop(decision_id, None)

            logger.info(
                "Decision timeout handled",
                decision_id=decision_id,
                default_used=response
            )

            return response

    async def get_pending_decisions(self, task_id: Optional[str] = None) -> list[Decision]:
        """Get all pending decisions, optionally filtered by task."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            query = select(Decision).where(Decision.status == DecisionStatus.PENDING)
            if task_id:
                query = query.where(Decision.task_id == task_id)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def cleanup_expired(self) -> int:
        """Clean up expired pending decisions. Returns count processed."""
        session_maker = get_session_maker()
        async with session_maker() as session:
            query = select(Decision).where(
                Decision.status == DecisionStatus.PENDING,
                Decision.timeout_at < datetime.utcnow()
            )
            result = await session.execute(query)
            decisions = list(result.scalars().all())

            count = 0
            for decision in decisions:
                decision.status = DecisionStatus.TIMEOUT
                decision.responded_at = datetime.utcnow()

                # Update task
                task = await session.get(Task, decision.task_id)
                if task and task.status == TaskStatus.BLOCKED:
                    task.status = TaskStatus.QUEUED

                # Signal waiting
                event = self._pending_decisions.pop(decision.id, None)
                if event:
                    event.set()

                count += 1

            await session.commit()
            return count

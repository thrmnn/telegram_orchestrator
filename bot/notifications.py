"""Proactive notification system for Telegram."""

from datetime import datetime
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
import structlog

from config import get_settings
from models import Task, Agent, Decision, TaskStatus, AgentStatus

logger = structlog.get_logger()


class NotificationService:
    """Service for sending proactive notifications to Telegram."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.settings = get_settings()

    def _get_chat_ids(self) -> list[int]:
        """Get authorized chat IDs for notifications."""
        return self.settings.telegram_authorized_chat_ids

    async def _send_to_all(
        self,
        message: str,
        parse_mode: str = ParseMode.MARKDOWN,
        reply_markup: Optional[InlineKeyboardMarkup] = None
    ):
        """Send a message to all authorized users."""
        for chat_id in self._get_chat_ids():
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.error("Failed to send notification", chat_id=chat_id, error=str(e))

    async def notify_task_started(self, task: Task, agent: Agent):
        """Notify when a task starts execution."""
        if not self.settings.notify_on_task_start:
            return

        message = (
            f"*Task Started*\n\n"
            f"*Agent:* {agent.name}\n"
            f"*Task:* {task.description[:100]}{'...' if len(task.description) > 100 else ''}\n"
            f"*Branch:* `{task.branch_name or 'pending'}`\n"
            f"*Priority:* {task.priority.value}\n\n"
            f"_Task ID: {task.id[:8]}_"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("View Logs", callback_data=f"logs_{task.id}"),
                InlineKeyboardButton("Pause", callback_data=f"pause_{task.id}")
            ]
        ])

        await self._send_to_all(message, reply_markup=keyboard)
        logger.info("Task start notification sent", task_id=task.id, agent=agent.name)

    async def notify_task_completed(self, task: Task, agent: Optional[Agent] = None):
        """Notify when a task completes successfully."""
        if not self.settings.notify_on_task_complete:
            return

        duration = ""
        if task.started_at and task.completed_at:
            delta = task.completed_at - task.started_at
            minutes = int(delta.total_seconds() / 60)
            duration = f"\n*Duration:* {minutes} minutes"

        pr_info = ""
        if task.pr_url:
            pr_info = f"\n*PR:* [#{task.pr_number}]({task.pr_url})"

        message = (
            f"*Task Completed*\n\n"
            f"*Task:* {task.description[:100]}{'...' if len(task.description) > 100 else ''}\n"
            f"*Status:* {task.status.value}"
            f"{duration}"
            f"{pr_info}\n"
            f"*Tokens:* {task.tokens_used:,}\n"
            f"*Cost:* ${task.estimated_cost_usd:.4f}\n\n"
            f"_Task ID: {task.id[:8]}_"
        )

        keyboard = None
        if task.pr_url:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("View PR", url=task.pr_url),
                    InlineKeyboardButton("Approve", callback_data=f"approve_{task.id}")
                ]
            ])

        await self._send_to_all(message, reply_markup=keyboard)
        logger.info("Task completion notification sent", task_id=task.id)

    async def notify_task_failed(self, task: Task, error: str):
        """Notify when a task fails."""
        if not self.settings.notify_on_error:
            return

        message = (
            f"*Task Failed*\n\n"
            f"*Task:* {task.description[:100]}{'...' if len(task.description) > 100 else ''}\n"
            f"*Error:*\n```\n{error[:500]}{'...' if len(error) > 500 else ''}\n```\n\n"
            f"_Task ID: {task.id[:8]}_"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("View Logs", callback_data=f"logs_{task.id}"),
                InlineKeyboardButton("Retry", callback_data=f"retry_{task.id}"),
                InlineKeyboardButton("Cancel", callback_data=f"cancel_{task.id}")
            ]
        ])

        await self._send_to_all(message, reply_markup=keyboard)
        logger.info("Task failure notification sent", task_id=task.id)

    async def notify_pr_ready(self, task: Task):
        """Notify when a PR is ready for review."""
        if not self.settings.notify_on_pr_ready:
            return

        message = (
            f"*Pull Request Ready*\n\n"
            f"*Task:* {task.description[:100]}{'...' if len(task.description) > 100 else ''}\n"
            f"*PR:* [#{task.pr_number}]({task.pr_url})\n"
            f"*Branch:* `{task.branch_name}`\n\n"
            f"Please review and approve the changes."
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("View PR", url=task.pr_url),
                InlineKeyboardButton("Approve & Merge", callback_data=f"merge_{task.id}")
            ],
            [
                InlineKeyboardButton("Request Changes", callback_data=f"changes_{task.id}")
            ]
        ])

        await self._send_to_all(message, reply_markup=keyboard)
        logger.info("PR ready notification sent", task_id=task.id, pr_number=task.pr_number)

    async def notify_decision_needed(self, decision: Decision, task: Task):
        """Notify when a human decision is required."""
        options_text = ""
        keyboard_buttons = []

        if decision.options:
            options = decision.options if isinstance(decision.options, list) else decision.options.get("options", [])
            options_text = "\n*Options:*\n"
            for i, opt in enumerate(options, 1):
                options_text += f"{i}. {opt}\n"
                keyboard_buttons.append(
                    InlineKeyboardButton(f"{i}", callback_data=f"decide_{decision.id}_{i}")
                )

        default_info = ""
        if decision.default_option:
            timeout_mins = self.settings.decision_timeout_seconds // 60
            default_info = f"\n_Default ({decision.default_option}) will be used in {timeout_mins} minutes if no response._"

        message = (
            f"*Decision Required*\n\n"
            f"*Task:* {task.description[:100]}{'...' if len(task.description) > 100 else ''}\n\n"
            f"*Question:*\n{decision.question}\n"
            f"{options_text}"
            f"\n*Context:*\n{decision.context[:300] if decision.context else 'None'}{'...' if decision.context and len(decision.context) > 300 else ''}"
            f"{default_info}\n\n"
            f"_Decision ID: {decision.id[:8]}_"
        )

        keyboard_rows = []
        if keyboard_buttons:
            # Split into rows of 4
            for i in range(0, len(keyboard_buttons), 4):
                keyboard_rows.append(keyboard_buttons[i:i+4])
        keyboard_rows.append([
            InlineKeyboardButton("Skip", callback_data=f"skip_{decision.id}"),
            InlineKeyboardButton("Custom Response", callback_data=f"custom_{decision.id}")
        ])

        await self._send_to_all(message, reply_markup=InlineKeyboardMarkup(keyboard_rows))
        logger.info("Decision notification sent", decision_id=decision.id, task_id=task.id)

    async def notify_agent_error(self, agent: Agent, error: str):
        """Notify when an agent encounters an error."""
        if not self.settings.notify_on_error:
            return

        message = (
            f"*Agent Error*\n\n"
            f"*Agent:* {agent.name}\n"
            f"*Status:* {agent.status.value}\n"
            f"*Error:*\n```\n{error[:500]}{'...' if len(error) > 500 else ''}\n```\n"
            f"*Error Count:* {agent.error_count}"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Restart Agent", callback_data=f"restart_{agent.id}"),
                InlineKeyboardButton("View Logs", callback_data=f"agent_logs_{agent.id}")
            ]
        ])

        await self._send_to_all(message, reply_markup=keyboard)
        logger.info("Agent error notification sent", agent_id=agent.id)

    async def notify_budget_warning(self, current_cost: float, budget: float, percent: float):
        """Notify when approaching budget limit."""
        message = (
            f"*Budget Warning*\n\n"
            f"*Current Spend:* ${current_cost:.2f}\n"
            f"*Daily Budget:* ${budget:.2f}\n"
            f"*Usage:* {percent:.1f}%\n\n"
            f"_Consider pausing tasks or increasing budget._"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Increase Budget", callback_data="budget_increase"),
                InlineKeyboardButton("Pause All", callback_data="pause_all")
            ]
        ])

        await self._send_to_all(message, reply_markup=keyboard)
        logger.warning("Budget warning sent", current=current_cost, budget=budget, percent=percent)

    async def send_daily_digest(self, digest: dict):
        """Send daily activity digest."""
        message = (
            f"*Daily Digest - {datetime.now().strftime('%Y-%m-%d')}*\n\n"
            f"*Tasks Completed:* {digest['tasks_completed']}\n"
            f"*Tasks Failed:* {digest['tasks_failed']}\n"
            f"*PRs Created:* {digest['prs_created']}\n"
            f"*PRs Merged:* {digest['prs_merged']}\n\n"
            f"*Total Tokens:* {digest['total_tokens']:,}\n"
            f"*Total Cost:* ${digest['total_cost']:.2f}\n\n"
            f"*Decisions Made:* {digest['decisions_made']}\n"
            f"*Avg Decision Time:* {digest['avg_decision_time']:.0f}s\n\n"
            f"*Agent Performance:*\n"
        )

        for agent in digest.get('agents', []):
            message += f"  - {agent['name']}: {agent['completed']} completed, {agent['failed']} failed\n"

        if digest.get('top_errors'):
            message += f"\n*Top Errors:*\n"
            for error in digest['top_errors'][:3]:
                message += f"  - {error[:100]}\n"

        await self._send_to_all(message)
        logger.info("Daily digest sent")

    async def send_custom_notification(self, title: str, body: str, chat_id: Optional[int] = None):
        """Send a custom notification."""
        message = f"*{title}*\n\n{body}"

        if chat_id:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error("Failed to send custom notification", chat_id=chat_id, error=str(e))
        else:
            await self._send_to_all(message)

"""Telegram bot command handlers."""

import os
import tempfile
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telegram.constants import ParseMode
import structlog

from config import get_settings, AutonomyMode
from models import (
    Task, Agent, Decision, TaskStatus, TaskPriority, AgentStatus, DecisionStatus,
    init_database, get_session_maker
)
from .auth import authorized_only, admin_only
from .notifications import NotificationService

logger = structlog.get_logger()

# Global references (set during initialization)
orchestrator = None
notification_service: Optional[NotificationService] = None


def set_orchestrator(orch):
    """Set the orchestrator reference."""
    global orchestrator
    orchestrator = orch


def set_notification_service(service: NotificationService):
    """Set the notification service reference."""
    global notification_service
    notification_service = service


# Command Handlers

@authorized_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    message = (
        f"Welcome to the Development Orchestrator, {user.first_name}!\n\n"
        "I can help you manage multiple Claude Code agents for autonomous development.\n\n"
        "*Available Commands:*\n"
        "/create <task> - Create a new development task\n"
        "/status - View system status\n"
        "/agents - List all agents\n"
        "/review - View pending PRs\n"
        "/approve <task_id> - Approve a PR\n"
        "/pause [task_id] - Pause tasks\n"
        "/cancel <task_id> - Cancel a task\n"
        "/logs [task_id] - View logs\n"
        "/setmode <mode> - Set autonomy mode\n"
        "/setbudget <usd> - Set daily budget\n"
        "/help - Show this help message\n\n"
        "_Send a voice message or text to quickly create a task!_"
    )
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


@authorized_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    message = (
        "*Development Orchestrator Help*\n\n"
        "*Task Management:*\n"
        "`/create <description>` - Create a new task\n"
        "  Example: `/create Add user authentication`\n"
        "`/cancel <task_id>` - Cancel a task\n"
        "`/pause [task_id]` - Pause task(s)\n\n"
        "*Monitoring:*\n"
        "`/status` - View overall system status\n"
        "`/agents` - List all agents and their status\n"
        "`/logs [task_id]` - View recent activity logs\n\n"
        "*Code Review:*\n"
        "`/review` - View pending pull requests\n"
        "`/approve <task_id>` - Approve and merge PR\n\n"
        "*Settings:*\n"
        "`/setmode <mode>` - Set autonomy mode\n"
        "  Modes: `full-auto`, `balanced`, `supervised`\n"
        "`/setbudget <usd>` - Set daily budget limit\n\n"
        "*Quick Tasks:*\n"
        "You can also send a text or voice message to create a task quickly.\n\n"
        "*Autonomy Modes:*\n"
        "- `full-auto`: Agents work autonomously\n"
        "- `balanced`: Ask for major decisions only\n"
        "- `supervised`: Ask before each step"
    )
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


@authorized_only
async def create_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /create command to create a new task."""
    if not context.args:
        await update.message.reply_text(
            "Please provide a task description.\n"
            "Usage: `/create <task description>`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    description = " ".join(context.args)
    chat_id = update.effective_chat.id
    message_id = update.message.message_id

    # Parse priority from description
    priority = TaskPriority.NORMAL
    if "[urgent]" in description.lower():
        priority = TaskPriority.URGENT
        description = description.replace("[urgent]", "").replace("[URGENT]", "").strip()
    elif "[high]" in description.lower():
        priority = TaskPriority.HIGH
        description = description.replace("[high]", "").replace("[HIGH]", "").strip()
    elif "[low]" in description.lower():
        priority = TaskPriority.LOW
        description = description.replace("[low]", "").replace("[LOW]", "").strip()

    try:
        if orchestrator:
            task = await orchestrator.create_task(
                description=description,
                chat_id=chat_id,
                message_id=message_id,
                priority=priority
            )

            await update.message.reply_text(
                f"*Task Created*\n\n"
                f"*ID:* `{task.id[:8]}`\n"
                f"*Description:* {task.description[:200]}\n"
                f"*Priority:* {task.priority.value}\n"
                f"*Status:* {task.status.value}\n\n"
                f"_Task queued for processing._",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "Orchestrator not initialized. Please try again later."
            )
    except Exception as e:
        logger.error("Failed to create task", error=str(e))
        await update.message.reply_text(f"Failed to create task: {str(e)}")


@authorized_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command to show system status."""
    try:
        if orchestrator:
            status = await orchestrator.get_system_status()

            settings = get_settings()
            budget_percent = (status["daily_cost"] / settings.daily_budget_usd * 100) if settings.daily_budget_usd > 0 else 0

            message = (
                f"*System Status*\n\n"
                f"*Agents:*\n"
                f"  Total: {status['total_agents']}\n"
                f"  Active: {status['active_agents']}\n"
                f"  Idle: {status['idle_agents']}\n\n"
                f"*Tasks:*\n"
                f"  Pending: {status['pending_tasks']}\n"
                f"  In Progress: {status['in_progress_tasks']}\n"
                f"  Completed Today: {status['completed_today']}\n"
                f"  Failed Today: {status['failed_today']}\n\n"
                f"*Decisions Pending:* {status['pending_decisions']}\n\n"
                f"*Budget:*\n"
                f"  Used: ${status['daily_cost']:.2f} / ${settings.daily_budget_usd:.2f}\n"
                f"  Usage: {budget_percent:.1f}%\n\n"
                f"*Mode:* {settings.autonomy_mode.value}"
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Refresh", callback_data="refresh_status"),
                    InlineKeyboardButton("View Agents", callback_data="view_agents")
                ]
            ])

            await update.message.reply_text(
                message,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text("Orchestrator not initialized.")
    except Exception as e:
        logger.error("Failed to get status", error=str(e))
        await update.message.reply_text(f"Failed to get status: {str(e)}")


@authorized_only
async def agents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /agents command to list all agents."""
    try:
        if orchestrator:
            agents = await orchestrator.get_agents()

            if not agents:
                await update.message.reply_text("No agents configured.")
                return

            message = "*Agents*\n\n"
            for agent in agents:
                status_emoji = {
                    AgentStatus.IDLE: "",
                    AgentStatus.BUSY: "",
                    AgentStatus.PAUSED: "",
                    AgentStatus.ERROR: "",
                    AgentStatus.OFFLINE: ""
                }.get(agent.status, "")

                task_info = ""
                if agent.current_task_id:
                    task_info = f"\n  Task: `{agent.current_task_id[:8]}`"

                message += (
                    f"{status_emoji} *{agent.name}*\n"
                    f"  Status: {agent.status.value}"
                    f"{task_info}\n"
                    f"  Completed: {agent.tasks_completed} | Failed: {agent.tasks_failed}\n"
                    f"  Cost: ${agent.total_cost_usd:.2f}\n\n"
                )

            await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("Orchestrator not initialized.")
    except Exception as e:
        logger.error("Failed to list agents", error=str(e))
        await update.message.reply_text(f"Failed to list agents: {str(e)}")


@authorized_only
async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /review command to show pending PRs."""
    try:
        if orchestrator:
            tasks_with_prs = await orchestrator.get_pending_prs()

            if not tasks_with_prs:
                await update.message.reply_text("No pending pull requests.")
                return

            message = "*Pending Pull Requests*\n\n"
            for task in tasks_with_prs:
                message += (
                    f"*{task.description[:50]}...*\n"
                    f"  PR: [#{task.pr_number}]({task.pr_url})\n"
                    f"  Branch: `{task.branch_name}`\n"
                    f"  ID: `{task.id[:8]}`\n\n"
                )

            await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("Orchestrator not initialized.")
    except Exception as e:
        logger.error("Failed to get pending PRs", error=str(e))
        await update.message.reply_text(f"Failed to get pending PRs: {str(e)}")


@authorized_only
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /approve command to approve and merge a PR."""
    if not context.args:
        await update.message.reply_text(
            "Please provide a task ID.\n"
            "Usage: `/approve <task_id>`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    task_id = context.args[0]

    try:
        if orchestrator:
            result = await orchestrator.approve_pr(task_id)
            if result["success"]:
                await update.message.reply_text(
                    f"*PR Approved and Merged*\n\n"
                    f"Task `{task_id[:8]}` has been merged successfully.",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text(
                    f"Failed to merge PR: {result['error']}"
                )
        else:
            await update.message.reply_text("Orchestrator not initialized.")
    except Exception as e:
        logger.error("Failed to approve PR", error=str(e), task_id=task_id)
        await update.message.reply_text(f"Failed to approve PR: {str(e)}")


@authorized_only
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pause command to pause tasks."""
    task_id = context.args[0] if context.args else None

    try:
        if orchestrator:
            if task_id:
                result = await orchestrator.pause_task(task_id)
                await update.message.reply_text(
                    f"Task `{task_id[:8]}` paused." if result else f"Failed to pause task.",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                result = await orchestrator.pause_all_tasks()
                await update.message.reply_text(
                    f"All tasks paused. {result['paused_count']} tasks affected."
                )
        else:
            await update.message.reply_text("Orchestrator not initialized.")
    except Exception as e:
        logger.error("Failed to pause", error=str(e))
        await update.message.reply_text(f"Failed to pause: {str(e)}")


@authorized_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command to cancel a task."""
    if not context.args:
        await update.message.reply_text(
            "Please provide a task ID.\n"
            "Usage: `/cancel <task_id>`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    task_id = context.args[0]

    try:
        if orchestrator:
            result = await orchestrator.cancel_task(task_id)
            if result:
                await update.message.reply_text(
                    f"Task `{task_id[:8]}` cancelled.",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text("Failed to cancel task.")
        else:
            await update.message.reply_text("Orchestrator not initialized.")
    except Exception as e:
        logger.error("Failed to cancel task", error=str(e), task_id=task_id)
        await update.message.reply_text(f"Failed to cancel task: {str(e)}")


@authorized_only
async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /logs command to view activity logs."""
    task_id = context.args[0] if context.args else None
    limit = 10

    try:
        if orchestrator:
            logs = await orchestrator.get_logs(task_id=task_id, limit=limit)

            if not logs:
                await update.message.reply_text("No logs found.")
                return

            message = f"*Recent Activity{' for task ' + task_id[:8] if task_id else ''}*\n\n"
            for log in logs:
                timestamp = log.timestamp.strftime("%H:%M:%S")
                message += f"`{timestamp}` [{log.level}] {log.action}\n"
                if log.details:
                    details = str(log.details)[:100]
                    message += f"  _{details}_\n"

            await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("Orchestrator not initialized.")
    except Exception as e:
        logger.error("Failed to get logs", error=str(e))
        await update.message.reply_text(f"Failed to get logs: {str(e)}")


@admin_only
async def setmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setmode command to change autonomy mode."""
    if not context.args:
        settings = get_settings()
        await update.message.reply_text(
            f"Current mode: `{settings.autonomy_mode.value}`\n\n"
            "Usage: `/setmode <mode>`\n"
            "Modes: `full-auto`, `balanced`, `supervised`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    mode = context.args[0].lower()
    valid_modes = {"full-auto", "balanced", "supervised"}

    if mode not in valid_modes:
        await update.message.reply_text(
            f"Invalid mode. Choose from: {', '.join(valid_modes)}"
        )
        return

    try:
        if orchestrator:
            await orchestrator.set_autonomy_mode(AutonomyMode(mode))
            await update.message.reply_text(
                f"Autonomy mode set to `{mode}`.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text("Orchestrator not initialized.")
    except Exception as e:
        logger.error("Failed to set mode", error=str(e))
        await update.message.reply_text(f"Failed to set mode: {str(e)}")


@admin_only
async def setbudget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setbudget command to change daily budget."""
    if not context.args:
        settings = get_settings()
        await update.message.reply_text(
            f"Current daily budget: ${settings.daily_budget_usd:.2f}\n\n"
            "Usage: `/setbudget <amount>`\n"
            "Example: `/setbudget 100`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        budget = float(context.args[0])
        if budget < 0:
            raise ValueError("Budget must be positive")

        if orchestrator:
            await orchestrator.set_daily_budget(budget)
            await update.message.reply_text(
                f"Daily budget set to ${budget:.2f}.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text("Orchestrator not initialized.")
    except ValueError as e:
        await update.message.reply_text(
            f"Invalid budget amount. Please provide a positive number."
        )
    except Exception as e:
        logger.error("Failed to set budget", error=str(e))
        await update.message.reply_text(f"Failed to set budget: {str(e)}")


# Message Handlers

@authorized_only
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages as quick task creation."""
    text = update.message.text

    # Skip if it looks like a command
    if text.startswith("/"):
        return

    chat_id = update.effective_chat.id
    message_id = update.message.message_id

    # Confirm task creation
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Create Task", callback_data=f"confirm_task_{message_id}"),
            InlineKeyboardButton("Cancel", callback_data="cancel_task_creation")
        ]
    ])

    # Store the task description temporarily
    context.user_data["pending_task"] = {
        "description": text,
        "chat_id": chat_id,
        "message_id": message_id
    }

    await update.message.reply_text(
        f"*Create task?*\n\n_{text[:200]}{'...' if len(text) > 200 else ''}_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard
    )


@authorized_only
async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages for task creation."""
    voice = update.message.voice
    chat_id = update.effective_chat.id

    await update.message.reply_text("Transcribing voice message...")

    try:
        # Download voice file
        voice_file = await context.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp_file:
            await voice_file.download_to_drive(tmp_file.name)
            tmp_path = tmp_file.name

        # Transcribe using whisper (if available)
        transcription = await transcribe_voice(tmp_path)

        # Clean up
        os.unlink(tmp_path)

        if transcription:
            context.user_data["pending_task"] = {
                "description": transcription,
                "chat_id": chat_id,
                "message_id": update.message.message_id,
                "voice_transcription": True
            }

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Create Task", callback_data=f"confirm_task_{update.message.message_id}"),
                    InlineKeyboardButton("Cancel", callback_data="cancel_task_creation")
                ]
            ])

            await update.message.reply_text(
                f"*Transcription:*\n_{transcription}_\n\nCreate task?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text("Failed to transcribe voice message.")

    except Exception as e:
        logger.error("Failed to process voice message", error=str(e))
        await update.message.reply_text(f"Failed to process voice message: {str(e)}")


async def transcribe_voice(file_path: str) -> Optional[str]:
    """Transcribe a voice file using Whisper."""
    try:
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(file_path)
        return result["text"]
    except ImportError:
        logger.warning("Whisper not available for voice transcription")
        return None
    except Exception as e:
        logger.error("Transcription failed", error=str(e))
        return None


# Callback Query Handlers

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data

    try:
        if data.startswith("confirm_task_"):
            await handle_confirm_task(query, context)
        elif data == "cancel_task_creation":
            await query.edit_message_text("Task creation cancelled.")
            context.user_data.pop("pending_task", None)
        elif data.startswith("logs_"):
            task_id = data.replace("logs_", "")
            await handle_view_logs(query, task_id)
        elif data.startswith("pause_"):
            task_id = data.replace("pause_", "")
            await handle_pause_task(query, task_id)
        elif data.startswith("retry_"):
            task_id = data.replace("retry_", "")
            await handle_retry_task(query, task_id)
        elif data.startswith("cancel_"):
            task_id = data.replace("cancel_", "")
            await handle_cancel_task(query, task_id)
        elif data.startswith("approve_"):
            task_id = data.replace("approve_", "")
            await handle_approve_task(query, task_id)
        elif data.startswith("merge_"):
            task_id = data.replace("merge_", "")
            await handle_merge_pr(query, task_id)
        elif data.startswith("decide_"):
            parts = data.split("_")
            decision_id = parts[1]
            option_index = int(parts[2])
            await handle_decision_response(query, decision_id, option_index)
        elif data.startswith("skip_"):
            decision_id = data.replace("skip_", "")
            await handle_skip_decision(query, decision_id)
        elif data.startswith("custom_"):
            decision_id = data.replace("custom_", "")
            await handle_custom_decision(query, context, decision_id)
        elif data == "refresh_status":
            await handle_refresh_status(query)
        elif data == "view_agents":
            await handle_view_agents(query)
        elif data.startswith("restart_"):
            agent_id = data.replace("restart_", "")
            await handle_restart_agent(query, agent_id)
        elif data == "pause_all":
            await handle_pause_all(query)
        elif data == "budget_increase":
            await handle_budget_increase(query)
        else:
            logger.warning("Unknown callback data", data=data)

    except Exception as e:
        logger.error("Callback handler error", error=str(e), data=data)
        await query.edit_message_text(f"Error: {str(e)}")


async def handle_confirm_task(query, context: ContextTypes.DEFAULT_TYPE):
    """Handle task confirmation callback."""
    pending = context.user_data.get("pending_task")
    if not pending:
        await query.edit_message_text("Task data expired. Please try again.")
        return

    try:
        if orchestrator:
            task = await orchestrator.create_task(
                description=pending["description"],
                chat_id=pending["chat_id"],
                message_id=pending["message_id"]
            )

            await query.edit_message_text(
                f"*Task Created*\n\n"
                f"*ID:* `{task.id[:8]}`\n"
                f"*Description:* {task.description[:200]}\n"
                f"*Status:* {task.status.value}\n\n"
                f"_Task queued for processing._",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_text("Orchestrator not initialized.")

        context.user_data.pop("pending_task", None)
    except Exception as e:
        logger.error("Failed to create task", error=str(e))
        await query.edit_message_text(f"Failed to create task: {str(e)}")


async def handle_view_logs(query, task_id: str):
    """Handle view logs callback."""
    if orchestrator:
        logs = await orchestrator.get_logs(task_id=task_id, limit=5)
        message = f"*Logs for {task_id[:8]}*\n\n"
        for log in logs:
            timestamp = log.timestamp.strftime("%H:%M:%S")
            message += f"`{timestamp}` {log.action}\n"
        await query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN)


async def handle_pause_task(query, task_id: str):
    """Handle pause task callback."""
    if orchestrator:
        result = await orchestrator.pause_task(task_id)
        await query.edit_message_text(
            f"Task `{task_id[:8]}` paused." if result else "Failed to pause task.",
            parse_mode=ParseMode.MARKDOWN
        )


async def handle_retry_task(query, task_id: str):
    """Handle retry task callback."""
    if orchestrator:
        result = await orchestrator.retry_task(task_id)
        await query.edit_message_text(
            f"Task `{task_id[:8]}` queued for retry." if result else "Failed to retry task.",
            parse_mode=ParseMode.MARKDOWN
        )


async def handle_cancel_task(query, task_id: str):
    """Handle cancel task callback."""
    if orchestrator:
        result = await orchestrator.cancel_task(task_id)
        await query.edit_message_text(
            f"Task `{task_id[:8]}` cancelled." if result else "Failed to cancel task.",
            parse_mode=ParseMode.MARKDOWN
        )


async def handle_approve_task(query, task_id: str):
    """Handle approve task callback."""
    await query.edit_message_text(
        f"Use `/approve {task_id[:8]}` to approve and merge the PR.",
        parse_mode=ParseMode.MARKDOWN
    )


async def handle_merge_pr(query, task_id: str):
    """Handle merge PR callback."""
    if orchestrator:
        result = await orchestrator.approve_pr(task_id)
        await query.edit_message_text(
            f"PR merged successfully!" if result["success"] else f"Failed to merge: {result['error']}",
            parse_mode=ParseMode.MARKDOWN
        )


async def handle_decision_response(query, decision_id: str, option_index: int):
    """Handle decision response callback."""
    if orchestrator:
        result = await orchestrator.respond_to_decision(decision_id, option_index=option_index)
        await query.edit_message_text(
            f"Decision recorded. Task will resume." if result else "Failed to record decision.",
            parse_mode=ParseMode.MARKDOWN
        )


async def handle_skip_decision(query, decision_id: str):
    """Handle skip decision callback."""
    if orchestrator:
        result = await orchestrator.skip_decision(decision_id)
        await query.edit_message_text(
            "Decision skipped. Default action will be used." if result else "Failed to skip decision.",
            parse_mode=ParseMode.MARKDOWN
        )


async def handle_custom_decision(query, context: ContextTypes.DEFAULT_TYPE, decision_id: str):
    """Handle custom decision response."""
    context.user_data["pending_decision"] = decision_id
    await query.edit_message_text(
        "Please type your custom response:",
        parse_mode=ParseMode.MARKDOWN
    )


async def handle_refresh_status(query):
    """Handle refresh status callback."""
    if orchestrator:
        status = await orchestrator.get_system_status()
        settings = get_settings()
        budget_percent = (status["daily_cost"] / settings.daily_budget_usd * 100) if settings.daily_budget_usd > 0 else 0

        message = (
            f"*System Status* (refreshed)\n\n"
            f"*Agents:* {status['active_agents']}/{status['total_agents']} active\n"
            f"*Tasks:* {status['pending_tasks']} pending, {status['in_progress_tasks']} in progress\n"
            f"*Today:* {status['completed_today']} completed, {status['failed_today']} failed\n"
            f"*Budget:* ${status['daily_cost']:.2f} / ${settings.daily_budget_usd:.2f} ({budget_percent:.1f}%)"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Refresh", callback_data="refresh_status"),
                InlineKeyboardButton("View Agents", callback_data="view_agents")
            ]
        ])

        await query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def handle_view_agents(query):
    """Handle view agents callback."""
    if orchestrator:
        agents = await orchestrator.get_agents()
        message = "*Agents*\n\n"
        for agent in agents:
            message += f"*{agent.name}*: {agent.status.value}\n"
        await query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN)


async def handle_restart_agent(query, agent_id: str):
    """Handle restart agent callback."""
    if orchestrator:
        result = await orchestrator.restart_agent(agent_id)
        await query.edit_message_text(
            f"Agent restart initiated." if result else "Failed to restart agent.",
            parse_mode=ParseMode.MARKDOWN
        )


async def handle_pause_all(query):
    """Handle pause all callback."""
    if orchestrator:
        result = await orchestrator.pause_all_tasks()
        await query.edit_message_text(
            f"All tasks paused. {result['paused_count']} tasks affected.",
            parse_mode=ParseMode.MARKDOWN
        )


async def handle_budget_increase(query):
    """Handle budget increase callback."""
    settings = get_settings()
    new_budget = settings.daily_budget_usd * 1.5
    if orchestrator:
        await orchestrator.set_daily_budget(new_budget)
        await query.edit_message_text(
            f"Budget increased to ${new_budget:.2f}",
            parse_mode=ParseMode.MARKDOWN
        )


# Error Handler

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors in the bot."""
    logger.error("Bot error", error=str(context.error), update=update)

    if update and update.effective_message:
        await update.effective_message.reply_text(
            "An error occurred. Please try again later."
        )


def create_bot_application(token: str) -> Application:
    """Create and configure the Telegram bot application."""
    application = Application.builder().token(token).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("create", create_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("agents", agents_command))
    application.add_handler(CommandHandler("review", review_command))
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("setmode", setmode_command))
    application.add_handler(CommandHandler("setbudget", setbudget_command))

    # Message handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    # Callback query handler
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Error handler
    application.add_error_handler(error_handler)

    return application

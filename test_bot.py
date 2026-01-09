#!/usr/bin/env python3
"""Test bot startup."""

import asyncio
import sys
from pathlib import Path

import structlog
from telegram.ext import Application

from config import load_settings
from models import init_database
from orchestrator import OrchestrationEngine
from bot import create_bot_application, set_orchestrator, set_notification_service, NotificationService

# Configure logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

logger = structlog.get_logger()

async def main():
    logger.info("1. Loading settings")
    config_path = Path("config/config.yaml")
    settings = load_settings(config_path if config_path.exists() else None)

    logger.info("2. Creating bot application")
    bot_app = create_bot_application(settings.telegram_bot_token)

    logger.info("3. Creating notification service")
    notification_service = NotificationService(bot_app.bot)
    set_notification_service(notification_service)

    logger.info("4. Creating orchestrator")
    orchestrator = OrchestrationEngine(
        on_task_started=lambda task, agent: logger.info("Task started", task=task.id),
        on_task_completed=lambda task: logger.info("Task completed", task=task.id),
        on_task_failed=lambda task, error: logger.error("Task failed", task=task.id, error=str(error)),
        on_decision_needed=lambda decision, task: logger.info("Decision needed"),
        on_pr_ready=lambda task: logger.info("PR ready")
    )

    logger.info("5. Setting orchestrator")
    set_orchestrator(orchestrator)

    logger.info("6. Starting orchestrator")
    await orchestrator.start()

    logger.info("7. Initializing bot")
    await bot_app.initialize()

    logger.info("8. Starting bot")
    await bot_app.start()

    logger.info("9. Starting polling")
    await bot_app.updater.start_polling(drop_pending_updates=True)

    logger.info("✅ Bot is now running!")
    logger.info(f"Bot username: @{(await bot_app.bot.get_me()).username}")

    # Send notification
    try:
        await notification_service.send_custom_notification(
            "Bot Started",
            "Development orchestrator is now online!"
        )
        logger.info("Startup notification sent")
    except Exception as e:
        logger.error("Failed to send notification", error=str(e))

    # Keep running
    logger.info("Bot is running. Press Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await bot_app.updater.stop()
        await bot_app.stop()
        await orchestrator.stop()
        await bot_app.shutdown()
        logger.info("Shutdown complete")

if __name__ == "__main__":
    asyncio.run(main())

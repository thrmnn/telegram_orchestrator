#!/usr/bin/env python3
"""Main entry point for the Telegram Development Orchestrator."""

import asyncio
import signal
import sys
from pathlib import Path

import structlog
from telegram.ext import Application

from config import get_settings, load_settings
from models import init_database
from orchestrator import OrchestrationEngine
from bot import (
    create_bot_application,
    set_orchestrator,
    set_notification_service,
    NotificationService
)

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


class OrchestratorApp:
    """Main application class."""

    def __init__(self):
        self.settings = None
        self.orchestrator: OrchestrationEngine = None
        self.bot_app: Application = None
        self.notification_service: NotificationService = None
        self._shutdown_event = asyncio.Event()

    async def start(self):
        """Start all components."""
        logger.info("Starting Telegram Development Orchestrator")

        # Load settings
        config_path = Path("config/config.yaml")
        self.settings = load_settings(config_path if config_path.exists() else None)

        # Validate required settings
        if not self.settings.telegram_bot_token:
            logger.error("TELEGRAM_BOT_TOKEN is required")
            sys.exit(1)

        if not self.settings.telegram_authorized_chat_ids:
            logger.warning("No authorized chat IDs configured - bot will reject all users")

        # Create bot application
        self.bot_app = create_bot_application(self.settings.telegram_bot_token)

        # Create notification service
        self.notification_service = NotificationService(self.bot_app.bot)
        set_notification_service(self.notification_service)

        # Create orchestration engine with callbacks
        self.orchestrator = OrchestrationEngine(
            on_task_started=self._on_task_started,
            on_task_completed=self._on_task_completed,
            on_task_failed=self._on_task_failed,
            on_decision_needed=self._on_decision_needed,
            on_pr_ready=self._on_pr_ready
        )

        # Wire up orchestrator to bot handlers
        set_orchestrator(self.orchestrator)

        # Start orchestrator
        await self.orchestrator.start()

        # Start bot
        await self.bot_app.initialize()
        await self.bot_app.start()
        await self.bot_app.updater.start_polling(drop_pending_updates=True)

        logger.info("All components started successfully")

        # Send startup notification
        await self.notification_service.send_custom_notification(
            "Orchestrator Started",
            "Development orchestrator is now online and ready to accept tasks."
        )

    async def stop(self):
        """Stop all components gracefully."""
        logger.info("Stopping Telegram Development Orchestrator")

        # Send shutdown notification
        if self.notification_service:
            try:
                await self.notification_service.send_custom_notification(
                    "Orchestrator Stopping",
                    "Development orchestrator is shutting down."
                )
            except Exception:
                pass

        # Stop bot
        if self.bot_app:
            try:
                await self.bot_app.updater.stop()
                await self.bot_app.stop()
                await self.bot_app.shutdown()
            except Exception as e:
                logger.error("Error stopping bot", error=str(e))

        # Stop orchestrator
        if self.orchestrator:
            try:
                await self.orchestrator.stop()
            except Exception as e:
                logger.error("Error stopping orchestrator", error=str(e))

        logger.info("Shutdown complete")

    async def run(self):
        """Run the application until shutdown."""
        await self.start()

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        await self.stop()

    def request_shutdown(self):
        """Request graceful shutdown."""
        self._shutdown_event.set()

    # Notification callbacks

    async def _on_task_started(self, task, agent):
        """Handle task started event."""
        if self.notification_service:
            await self.notification_service.notify_task_started(task, agent)

    async def _on_task_completed(self, task):
        """Handle task completed event."""
        if self.notification_service:
            await self.notification_service.notify_task_completed(task)

    async def _on_task_failed(self, task, error):
        """Handle task failed event."""
        if self.notification_service:
            await self.notification_service.notify_task_failed(task, error)

    async def _on_decision_needed(self, decision, task):
        """Handle decision needed event."""
        if self.notification_service:
            await self.notification_service.notify_decision_needed(decision, task)

    async def _on_pr_ready(self, task):
        """Handle PR ready event."""
        if self.notification_service:
            await self.notification_service.notify_pr_ready(task)


def main():
    """Main entry point."""
    try:
        app = OrchestratorApp()

        # Handle signals
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def signal_handler(sig):
            logger.info("Received signal", signal=sig)
            app.request_shutdown()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))

        try:
            logger.info("Starting event loop")
            loop.run_until_complete(app.run())
            logger.info("Event loop completed")
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
            loop.run_until_complete(app.stop())
        except Exception as e:
            logger.error("Fatal error in main loop", error=str(e), exc_info=True)
            raise
        finally:
            loop.close()
            logger.info("Event loop closed")
    except Exception as e:
        logger.error("Fatal error in main()", error=str(e), exc_info=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

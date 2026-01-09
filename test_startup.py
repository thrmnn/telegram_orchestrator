#!/usr/bin/env python3
"""Debug bot startup step by step."""

import asyncio
import sys

print("="*60)
print("STARTUP DEBUG")
print("="*60)

print("\n[1/10] Importing modules...")
from pathlib import Path
import structlog
from telegram.ext import Application
print("    ✓ Basic imports")

from config import load_settings, get_settings
print("    ✓ Config imports")

from models import init_database, get_session_maker
print("    ✓ Model imports")

from orchestrator import OrchestrationEngine
print("    ✓ Orchestrator imports")

from bot import create_bot_application, set_orchestrator, set_notification_service, NotificationService
print("    ✓ Bot imports")

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
    print("\n[2/10] Loading settings...")
    config_path = Path("config/config.yaml")
    settings = load_settings(config_path if config_path.exists() else None)
    print(f"    ✓ Settings loaded")
    print(f"      - Token: {settings.telegram_bot_token[:20]}...")
    print(f"      - DB: {settings.database_url}")
    print(f"      - Agents: {settings.agent_count}")

    print("\n[3/10] Initializing database...")
    await init_database(settings.database_url)
    print("    ✓ Database initialized")

    print("\n[4/10] Creating bot application...")
    bot_app = create_bot_application(settings.telegram_bot_token)
    print("    ✓ Bot application created")

    print("\n[5/10] Creating notification service...")
    notification_service = NotificationService(bot_app.bot)
    set_notification_service(notification_service)
    print("    ✓ Notification service created")

    print("\n[6/10] Creating orchestrator (without starting)...")
    orchestrator = OrchestrationEngine(
        on_task_started=lambda task, agent: None,
        on_task_completed=lambda task: None,
        on_task_failed=lambda task, error: None,
        on_decision_needed=lambda decision, task: None,
        on_pr_ready=lambda task: None
    )
    print("    ✓ Orchestrator created")

    print("\n[7/10] Setting orchestrator in bot...")
    set_orchestrator(orchestrator)
    print("    ✓ Orchestrator set")

    print("\n[8/10] Starting orchestrator (THIS MAY HANG)...")
    sys.stdout.flush()
    await orchestrator.start()
    print("    ✓ Orchestrator started")

    print("\n[9/10] Initializing and starting bot...")
    await bot_app.initialize()
    print("    ✓ Bot initialized")
    await bot_app.start()
    print("    ✓ Bot started")

    print("\n[10/10] Starting polling...")
    await bot_app.updater.start_polling(drop_pending_updates=True)
    print("    ✓ Polling started")

    print("\n" + "="*60)
    print("✅ ALL STARTUP STEPS COMPLETE!")
    print("="*60)

    # Get bot info
    me = await bot_app.bot.get_me()
    print(f"\nBot is running: @{me.username}")
    print("Press Ctrl+C to stop...")

    # Keep running indefinitely
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down...")
        await bot_app.updater.stop()
        await bot_app.stop()
        await orchestrator.stop()
        await bot_app.shutdown()
        print("Shutdown complete")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()

"""Authentication and authorization for Telegram bot."""

from functools import wraps
from typing import Callable, Any

from telegram import Update
from telegram.ext import ContextTypes
import structlog

from config import get_settings

logger = structlog.get_logger()


def authorized_only(func: Callable) -> Callable:
    """Decorator to restrict commands to authorized users only."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs) -> Any:
        settings = get_settings()
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None

        # Check if user is authorized
        if user_id not in settings.telegram_authorized_chat_ids and chat_id not in settings.telegram_authorized_chat_ids:
            logger.warning(
                "Unauthorized access attempt",
                user_id=user_id,
                chat_id=chat_id,
                command=func.__name__
            )
            if update.message:
                await update.message.reply_text(
                    "You are not authorized to use this bot. "
                    "Please contact the administrator."
                )
            return None

        logger.info(
            "Authorized command",
            user_id=user_id,
            chat_id=chat_id,
            command=func.__name__
        )
        return await func(update, context, *args, **kwargs)

    return wrapper


def admin_only(func: Callable) -> Callable:
    """Decorator for admin-only commands (first user in authorized list)."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs) -> Any:
        settings = get_settings()
        user_id = update.effective_user.id if update.effective_user else None

        # First user in the list is considered admin
        if not settings.telegram_authorized_chat_ids or user_id != settings.telegram_authorized_chat_ids[0]:
            logger.warning(
                "Non-admin access attempt",
                user_id=user_id,
                command=func.__name__
            )
            if update.message:
                await update.message.reply_text(
                    "This command requires administrator privileges."
                )
            return None

        return await func(update, context, *args, **kwargs)

    return wrapper


def is_authorized(user_id: int) -> bool:
    """Check if a user ID is authorized."""
    settings = get_settings()
    return user_id in settings.telegram_authorized_chat_ids


def is_admin(user_id: int) -> bool:
    """Check if a user ID is an admin."""
    settings = get_settings()
    return (
        settings.telegram_authorized_chat_ids and
        user_id == settings.telegram_authorized_chat_ids[0]
    )


def get_authorized_chat_ids() -> list[int]:
    """Get list of authorized chat IDs."""
    settings = get_settings()
    return settings.telegram_authorized_chat_ids

"""Configuration management for the Telegram Orchestrator."""

from pathlib import Path
from typing import Optional
from enum import Enum

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import yaml


class AutonomyMode(str, Enum):
    """Agent autonomy levels."""
    FULL_AUTO = "full-auto"
    BALANCED = "balanced"
    SUPERVISED = "supervised"


class Settings(BaseSettings):
    """Application settings loaded from environment and config file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Telegram Configuration
    telegram_bot_token: str = Field(..., description="Telegram Bot API token")
    telegram_authorized_chat_ids: list[int] = Field(
        default_factory=list,
        description="List of authorized Telegram chat IDs"
    )

    # Database Configuration
    database_url: str = Field(
        default="sqlite+aiosqlite:///./orchestrator.db",
        description="Database connection URL"
    )

    # Redis Configuration
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL"
    )

    # Agent Configuration
    agent_count: int = Field(default=3, ge=1, le=10, description="Number of Claude Code agents")
    agent_workspace_base: Path = Field(
        default=Path("./workspaces"),
        description="Base directory for agent workspaces"
    )
    agent_timeout_seconds: int = Field(
        default=3600,
        description="Maximum time for a single agent task"
    )

    # GitHub Configuration
    github_token: Optional[str] = Field(default=None, description="GitHub personal access token")
    github_repo: Optional[str] = Field(default=None, description="Default GitHub repository (owner/repo)")
    github_base_branch: str = Field(default="main", description="Default base branch for PRs")

    # Orchestration Settings
    autonomy_mode: AutonomyMode = Field(
        default=AutonomyMode.BALANCED,
        description="Agent autonomy level"
    )
    max_parallel_tasks: int = Field(default=3, description="Maximum parallel tasks")
    decision_timeout_seconds: int = Field(
        default=3600,
        description="Timeout for human decisions before using default"
    )

    # Budget and Limits
    daily_budget_usd: float = Field(default=50.0, description="Daily API budget in USD")
    max_tokens_per_task: int = Field(default=100000, description="Maximum tokens per task")

    # Notification Settings
    notify_on_task_start: bool = Field(default=True)
    notify_on_task_complete: bool = Field(default=True)
    notify_on_error: bool = Field(default=True)
    notify_on_pr_ready: bool = Field(default=True)
    daily_digest_hour: int = Field(default=18, ge=0, le=23, description="Hour for daily digest (24h)")

    # Logging
    log_level: str = Field(default="INFO")
    log_file: Optional[Path] = Field(default=None)

    @field_validator("agent_workspace_base", mode="before")
    @classmethod
    def validate_workspace_path(cls, v):
        return Path(v) if isinstance(v, str) else v

    @field_validator("telegram_authorized_chat_ids", mode="before")
    @classmethod
    def parse_chat_ids(cls, v):
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        elif isinstance(v, int):
            return [v]
        elif isinstance(v, list):
            return v
        return []


def load_settings(config_path: Optional[Path] = None) -> Settings:
    """Load settings from environment and optional YAML config file."""
    config_data = {}

    if config_path and config_path.exists():
        with open(config_path) as f:
            config_data = yaml.safe_load(f) or {}

    # Flatten nested config for pydantic
    flat_config = {}
    for section, values in config_data.items():
        if isinstance(values, dict):
            for key, value in values.items():
                flat_config[f"{section}_{key}"] = value
        else:
            flat_config[section] = values

    return Settings(**flat_config)


# Global settings instance
settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get the global settings instance."""
    global settings
    if settings is None:
        config_path = Path("config/config.yaml")
        settings = load_settings(config_path if config_path.exists() else None)
    return settings

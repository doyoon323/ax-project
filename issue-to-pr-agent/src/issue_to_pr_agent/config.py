from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SERVICE_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or `.env`."""

    gemini_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    github_webhook_secret: SecretStr = SecretStr("")
    github_repository: str
    workspace_path: Path

    issue_source: Literal["webhook", "poll"] = "webhook"
    poll_interval_seconds: float = 15.0
    llm_model: str = "gemini/gemini-3.6-flash"
    llm_fallback_model: str | None = None
    llm_api_base: str | None = None
    llm_retries: int = 2
    base_branch: str = "main"
    max_turns: int = 3
    turn_delay_seconds: float = 4.1
    required_issue_label: str = "ai-fix"
    allowed_author_associations: str = "OWNER,MEMBER,COLLABORATOR"
    publish_enabled: bool = False
    fetch_before_run: bool = True
    require_verification: bool = True
    keep_failed_worktree: bool = False
    worktree_root: Path = Path("/tmp/issue-to-pr-agent/worktrees")
    state_db_path: Path = Path(".state/jobs.sqlite3")
    command_timeout_seconds: int = 30
    max_output_chars: int = 1_000
    max_output_lines: int = 50
    github_api_url: str = "https://api.github.com"
    github_api_version: str = "2022-11-28"

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("github_repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        parts = value.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("GITHUB_REPOSITORY must use the owner/repository form")
        return value

    @field_validator("max_turns")
    @classmethod
    def enforce_three_turn_cap(cls, value: int) -> int:
        if value != 3:
            raise ValueError("MAX_TURNS must be 3 for the fixed diagnose/patch/verify workflow")
        return value

    @field_validator("llm_retries")
    @classmethod
    def bound_llm_retries(cls, value: int) -> int:
        if not 0 <= value <= 3:
            raise ValueError("LLM_RETRIES must be between 0 and 3")
        return value

    @field_validator("turn_delay_seconds")
    @classmethod
    def enforce_minimum_delay(cls, value: float) -> float:
        if value < 4.0:
            raise ValueError("TURN_DELAY_SECONDS must be at least 4.0")
        return value

    @field_validator("poll_interval_seconds")
    @classmethod
    def enforce_poll_interval(cls, value: float) -> float:
        if value < 5.0:
            raise ValueError("POLL_INTERVAL_SECONDS must be at least 5.0")
        return value

    @model_validator(mode="after")
    def require_gemini_key_for_gemini_model(self) -> Settings:
        if self.llm_model.startswith("gemini/") and not self.gemini_api_key.get_secret_value():
            raise ValueError("GEMINI_API_KEY is required when LLM_MODEL uses the Gemini provider")
        if (
            self.llm_fallback_model
            and self.llm_fallback_model.startswith("groq/")
            and not self.groq_api_key.get_secret_value()
        ):
            raise ValueError(
                "GROQ_API_KEY is required when LLM_FALLBACK_MODEL uses the Groq provider"
            )
        if self.issue_source == "webhook" and not self.github_webhook_secret.get_secret_value():
            raise ValueError("GITHUB_WEBHOOK_SECRET is required in webhook mode")
        return self

    @property
    def allowed_associations(self) -> frozenset[str]:
        return frozenset(
            item.strip().upper()
            for item in self.allowed_author_associations.split(",")
            if item.strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # The parent project keeps the existing LLM secret; this service keeps only
    # repository-specific overrides in its own ignored .env file.
    return Settings(  # type: ignore[call-arg]
        _env_file=(_SERVICE_ROOT.parent / ".env", _SERVICE_ROOT / ".env")
    )

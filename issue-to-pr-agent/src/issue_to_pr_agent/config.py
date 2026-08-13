from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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
    llm_model: str = "gemini/gemini-3.1-pro-preview"
    llm_fallback_model: str | None = None
    llm_api_base: str | None = None
    llm_retries: int = 2
    llm_max_output_tokens: int = 4_096
    base_branch: str = "main"
    max_turns: int = 3
    turn_delay_seconds: float = 4.1
    required_issue_label: str = "ai-fix"
    allowed_author_associations: str = "OWNER,MEMBER,COLLABORATOR"
    publish_enabled: bool = False
    github_expected_login: str = ""
    git_author_name: str = "Issue-to-PR Agent"
    git_author_email: str = "issue-to-pr-agent@users.noreply.github.com"
    fetch_before_run: bool = True
    require_verification: bool = True
    required_verification_commands: list[list[str]] = Field(
        default_factory=lambda: [["python", "-m", "unittest", "discover", "-s", "tests", "-v"]]
    )
    verification_backend: Literal["docker", "host"] = "docker"
    verification_container_image: str = "python:3.13-slim"
    allow_host_verification: bool = False
    verification_timeout_seconds: int = 120
    keep_failed_worktree: bool = False
    job_max_attempts: int = 2
    job_retry_delay_seconds: float = 10.0
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

    @field_validator("llm_max_output_tokens")
    @classmethod
    def bound_llm_output(cls, value: int) -> int:
        if not 512 <= value <= 16_384:
            raise ValueError("LLM_MAX_OUTPUT_TOKENS must be between 512 and 16384")
        return value

    @field_validator("job_max_attempts")
    @classmethod
    def bound_job_attempts(cls, value: int) -> int:
        if not 1 <= value <= 5:
            raise ValueError("JOB_MAX_ATTEMPTS must be between 1 and 5")
        return value

    @field_validator("job_retry_delay_seconds")
    @classmethod
    def bound_job_retry_delay(cls, value: float) -> float:
        if not 0 <= value <= 300:
            raise ValueError("JOB_RETRY_DELAY_SECONDS must be between 0 and 300")
        return value

    @field_validator("verification_timeout_seconds")
    @classmethod
    def bound_verification_timeout(cls, value: int) -> int:
        if not 10 <= value <= 900:
            raise ValueError("VERIFICATION_TIMEOUT_SECONDS must be between 10 and 900")
        return value

    @field_validator("required_verification_commands")
    @classmethod
    def validate_required_verification_commands(cls, value: list[list[str]]) -> list[list[str]]:
        if not value:
            raise ValueError("REQUIRED_VERIFICATION_COMMANDS must contain at least one command")
        for command in value:
            if not command or len(command) > 30 or any(not item.strip() for item in command):
                raise ValueError("each required verification command must be a non-empty argv list")
        return value

    @field_validator("github_expected_login", "git_author_name", "git_author_email")
    @classmethod
    def reject_identity_control_characters(cls, value: str) -> str:
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("identity values cannot contain control characters")
        return value.strip()

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
        if self.publish_enabled and not self.github_expected_login:
            raise ValueError("GITHUB_EXPECTED_LOGIN is required when PUBLISH_ENABLED=true")
        if self.verification_backend == "host" and not self.allow_host_verification:
            raise ValueError(
                "ALLOW_HOST_VERIFICATION=true is required for the unsafe host verification backend"
            )
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

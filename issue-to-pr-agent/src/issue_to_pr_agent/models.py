from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Phase = Literal["diagnose", "patch", "verify"]


class FileEdit(BaseModel):
    """An exact, deterministic SEARCH/REPLACE edit."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["replace", "create", "append"] = "replace"
    path: str = Field(min_length=1, max_length=300)
    search: str = Field(max_length=50_000)
    replace: str = Field(max_length=50_000)


class AgentDecision(BaseModel):
    """The only response format accepted from the LLM."""

    model_config = ConfigDict(extra="forbid")

    phase: Phase
    note: str = Field(min_length=1, max_length=500)
    commands: list[list[str]] = Field(default_factory=list, max_length=5)
    edits: list[FileEdit] = Field(default_factory=list, max_length=8)
    finish: bool = False
    summary: str = Field(default="", max_length=2_000)
    pr_title: str = Field(default="", max_length=200)
    pr_body: str = Field(default="", max_length=5_000)


@dataclass(frozen=True)
class IssueTask:
    delivery_id: str
    repository: str
    number: int
    title: str
    body: str
    author: str
    author_association: str


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    return_code: int
    output: str
    timed_out: bool = False
    is_verification: bool = False

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and self.return_code == 0


@dataclass
class AgentRunResult:
    success: bool
    summary: str
    pr_title: str = ""
    pr_body: str = ""
    verification_results: list[CommandResult] = field(default_factory=list)
    baseline_verification_results: list[CommandResult] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)
    model_history: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    correction_cycles: int = 0
    duration_seconds: float = 0.0
    localization_candidates: list[str] = field(default_factory=list)
    localization_scanned_files: int = 0
    workspace: Path | None = None

"""Result types returned by the Code Impact Agent tools."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from code_impact.db_builder.records import FunctionRecord


@dataclass(frozen=True)
class ImpactFunction:
    function: FunctionRecord
    depth: int
    called_function_id: str


@dataclass(frozen=True)
class TestRecommendation:
    function: FunctionRecord
    called_function_id: str


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    kind: str
    file_path: str
    line: int
    description: str
    expected_symbol: str | None = None
    valid: bool | None = None

    def mark_valid(self, valid: bool) -> Evidence:
        return replace(self, valid=valid)


@dataclass(frozen=True)
class ToolResult:
    items: list[Any]
    evidence: list[Evidence]
    total_count: int
    truncated: bool

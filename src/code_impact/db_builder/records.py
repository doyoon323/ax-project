"""Data records created while building the code relationship database."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FunctionRecord:
    function_id: str
    module_name: str
    function_name: str
    file_path: str
    start_line: int
    end_line: int
    is_test: bool


@dataclass(frozen=True)
class CallRecord:
    caller_id: str
    callee_id: str
    file_path: str
    line: int


@dataclass(frozen=True)
class ChangedRange:
    file_path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ChangedFunction:
    function: FunctionRecord
    changed_line: int

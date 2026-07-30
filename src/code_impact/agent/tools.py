"""Deterministic tools used by the Code Impact Agent."""

from __future__ import annotations

from code_impact.agent.results import (
    Evidence,
    ImpactFunction,
    TestRecommendation,
    ToolResult,
)
from code_impact.db_builder.builder import build_commit_index
from code_impact.db_builder.git_reader import GitRepository
from code_impact.db_builder.sqlite_db import AnalysisDatabase


class AnalysisTools:
    def __init__(
        self,
        repository: GitRepository,
        database: AnalysisDatabase,
    ) -> None:
        self.repository = repository
        self.database = database
        self.commit: str | None = None

    def prepare_commit(self, ref: str) -> str:
        self.commit = build_commit_index(self.repository, self.database, ref)
        return self.commit

    def get_changed_functions(self, limit: int = 20) -> ToolResult:
        changed = self.database.list_changed_functions()
        selected = changed[:limit]
        evidence = [
            Evidence(
                evidence_id=f"CHG-{index:03d}",
                kind="changed_line",
                file_path=item.function.file_path,
                line=item.changed_line,
                description=(f"{item.function.function_id} 함수 범위에 Git 변경 라인이 있습니다."),
            )
            for index, item in enumerate(selected, start=1)
        ]
        return ToolResult(
            items=selected,
            evidence=evidence,
            total_count=len(changed),
            truncated=len(changed) > limit,
        )

    def get_callers(
        self,
        function_ids: list[str],
        max_depth: int = 2,
        limit: int = 20,
    ) -> ToolResult:
        visited = set(function_ids)
        frontier = list(function_ids)
        impacts: list[ImpactFunction] = []
        call_evidence: list[Evidence] = []

        for depth in range(1, max_depth + 1):
            next_frontier: list[str] = []
            for caller, call in self.database.callers_of(frontier):
                if caller.is_test or caller.function_id in visited:
                    continue
                visited.add(caller.function_id)
                next_frontier.append(caller.function_id)
                impacts.append(
                    ImpactFunction(
                        function=caller,
                        depth=depth,
                        called_function_id=call.callee_id,
                    )
                )
                callee_name = call.callee_id.rsplit("::", maxsplit=1)[-1]
                call_evidence.append(
                    Evidence(
                        evidence_id=f"CALL-{len(call_evidence) + 1:03d}",
                        kind="call",
                        file_path=call.file_path,
                        line=call.line,
                        description=f"{caller.function_id} → {call.callee_id} 호출",
                        expected_symbol=callee_name,
                    )
                )
            frontier = next_frontier
            if not frontier:
                break

        return ToolResult(
            items=impacts[:limit],
            evidence=call_evidence[:limit],
            total_count=len(impacts),
            truncated=len(impacts) > limit,
        )

    def find_related_tests(
        self,
        function_ids: list[str],
        limit: int = 20,
    ) -> ToolResult:
        recommendations: list[TestRecommendation] = []
        test_evidence: list[Evidence] = []
        seen_tests: set[str] = set()

        for test_function, call in self.database.test_callers_of(function_ids):
            if test_function.function_id in seen_tests:
                continue
            seen_tests.add(test_function.function_id)
            recommendations.append(
                TestRecommendation(
                    function=test_function,
                    called_function_id=call.callee_id,
                )
            )
            callee_name = call.callee_id.rsplit("::", maxsplit=1)[-1]
            test_evidence.append(
                Evidence(
                    evidence_id=f"TEST-{len(test_evidence) + 1:03d}",
                    kind="test_call",
                    file_path=call.file_path,
                    line=call.line,
                    description=f"{test_function.function_id} → {call.callee_id} 호출",
                    expected_symbol=callee_name,
                )
            )

        return ToolResult(
            items=recommendations[:limit],
            evidence=test_evidence[:limit],
            total_count=len(recommendations),
            truncated=len(recommendations) > limit,
        )

    def verify_evidence(self, evidence: list[Evidence]) -> list[Evidence]:
        if not self.commit:
            raise RuntimeError("prepare_commit must run before evidence verification.")

        verified: list[Evidence] = []
        for item in evidence:
            source_line = self.repository.line_at(
                self.commit,
                item.file_path,
                item.line,
            )
            valid = source_line is not None and bool(source_line.strip())
            if item.expected_symbol:
                valid = valid and item.expected_symbol in source_line
            verified.append(item.mark_valid(valid))
        return verified

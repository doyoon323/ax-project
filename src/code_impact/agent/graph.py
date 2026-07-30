"""LangGraph workflow that coordinates the code analysis tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from code_impact.agent.llm import (
    GeminiLanguageModel,
    GeminiResponseError,
    GroundedSummary,
    LanguageModel,
    QuestionIntent,
)
from code_impact.agent.report import render_report
from code_impact.agent.results import Evidence, ToolResult
from code_impact.agent.tools import AnalysisTools
from code_impact.db_builder.git_reader import GitRepository
from code_impact.db_builder.sqlite_db import AnalysisDatabase


class AgentState(TypedDict, total=False):
    question: str
    intent: QuestionIntent
    commit_ref: str
    commit: str
    changed: ToolResult
    impacts: ToolResult
    tests: ToolResult
    evidence: list[Evidence]
    trace: list[str]
    report: str
    error: str
    warning: str


class CodeImpactAgent:
    def __init__(
        self,
        repo_path: str | Path,
        db_path: str | Path,
        language_model: LanguageModel | None = None,
    ) -> None:
        self.repository = GitRepository(repo_path)
        self.database = AnalysisDatabase(db_path)
        self.tools = AnalysisTools(self.repository, self.database)
        self.language_model = language_model or GeminiLanguageModel.from_env()

    @staticmethod
    def _trace(state: AgentState, message: str) -> list[str]:
        return [*state.get("trace", []), message]

    def _parse_question(self, state: AgentState) -> AgentState:
        try:
            intent = self.language_model.interpret_question(state["question"])
        except GeminiResponseError as error:
            return {
                "error": str(error),
                "trace": self._trace(state, "Gemini 질문 해석 실패"),
            }

        try:
            self.repository.resolve_ref(intent.commit_ref)
        except Exception:
            return {
                "error": (
                    f"Gemini가 추출한 Git ref '{intent.commit_ref}'를 저장소에서 찾지 못했습니다."
                ),
                "trace": self._trace(state, "Git ref 검증 실패"),
            }
        return {
            "intent": intent,
            "commit_ref": intent.commit_ref,
            "trace": self._trace(
                state,
                (
                    f"Gemini 질문 해석: Git ref={intent.commit_ref}, "
                    f"영향 함수={'조회' if intent.include_impacts else '생략'}, "
                    f"테스트={'조회' if intent.include_tests else '생략'}"
                ),
            ),
        }

    def _get_changed_functions(self, state: AgentState) -> AgentState:
        commit = self.tools.prepare_commit(state["commit_ref"])
        result = self.tools.get_changed_functions()
        return {
            "commit": commit,
            "changed": result,
            "trace": self._trace(
                state,
                f"get_changed_functions 호출: {result.total_count}개",
            ),
        }

    def _get_callers(self, state: AgentState) -> AgentState:
        function_ids = [item.function.function_id for item in state["changed"].items]
        result = self.tools.get_callers(function_ids, max_depth=2)
        return {
            "impacts": result,
            "trace": self._trace(
                state,
                f"get_callers 호출: {result.total_count}개",
            ),
        }

    def _find_tests(self, state: AgentState) -> AgentState:
        function_ids = [item.function.function_id for item in state["changed"].items]
        function_ids.extend(
            item.function.function_id
            for item in state.get("impacts", ToolResult([], [], 0, False)).items
        )
        result = self.tools.find_related_tests(function_ids)
        return {
            "tests": result,
            "trace": self._trace(
                state,
                f"find_related_tests 호출: {result.total_count}개",
            ),
        }

    def _verify(self, state: AgentState) -> AgentState:
        evidence = [
            *state.get("changed", ToolResult([], [], 0, False)).evidence,
            *state.get("impacts", ToolResult([], [], 0, False)).evidence,
            *state.get("tests", ToolResult([], [], 0, False)).evidence,
        ]
        verified = self.tools.verify_evidence(evidence)
        valid_count = sum(item.valid is True for item in verified)
        update: AgentState = {
            "evidence": verified,
            "trace": self._trace(
                state,
                f"verify_evidence 호출: {valid_count}/{len(verified)}개 유효",
            ),
        }
        if valid_count != len(verified):
            update["error"] = (
                "일부 파일·라인 근거가 현재 커밋과 일치하지 않아 영향 보고서를 생성하지 않았습니다."
            )
        return update

    @staticmethod
    def _function_payload(item: Any) -> dict[str, Any]:
        function = item.function
        payload = {
            "function_id": function.function_id,
            "file_path": function.file_path,
            "start_line": function.start_line,
            "end_line": function.end_line,
        }
        if hasattr(item, "depth"):
            payload["depth"] = item.depth
        return payload

    def _summary_context(self, state: AgentState) -> dict[str, Any]:
        valid_evidence = [item for item in state.get("evidence", []) if item.valid]
        return {
            "commit": state["commit"],
            "changed_functions": [self._function_payload(item) for item in state["changed"].items],
            "impact_functions": [
                self._function_payload(item)
                for item in state.get("impacts", ToolResult([], [], 0, False)).items
            ],
            "recommended_tests": [
                self._function_payload(item)
                for item in state.get("tests", ToolResult([], [], 0, False)).items
            ],
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "file_path": item.file_path,
                    "line": item.line,
                    "description": item.description,
                }
                for item in valid_evidence
            ],
            "allowed_evidence_ids": [item.evidence_id for item in valid_evidence],
            "limits": {
                "impact_depth": 2,
                "impact_is_not_failure_prediction": True,
            },
        }

    def _report(self, state: AgentState) -> AgentState:
        summary: GroundedSummary | None = None
        warning = state.get("warning")
        if not state.get("error") and state.get("changed") and state["changed"].items:
            try:
                summary = self.language_model.summarize_report(self._summary_context(state))
            except GeminiResponseError as error:
                warning = str(error)

        report = render_report(
            commit=state.get("commit"),
            changed=state.get("changed"),
            impacts=state.get("impacts"),
            tests=state.get("tests"),
            evidence=state.get("evidence", []),
            summary=summary.summary if summary else None,
            summary_evidence_ids=summary.evidence_ids if summary else None,
            warning=warning,
            error=state.get("error"),
        )
        update: AgentState = {
            "report": report,
            "trace": self._trace(
                state,
                ("Gemini 근거 기반 요약과 보고서 생성" if summary else "Tool 결과로 보고서 생성"),
            ),
        }
        if warning:
            update["warning"] = warning
        return update

    @staticmethod
    def _after_parse(state: AgentState) -> str:
        return "report" if state.get("error") else "changed"

    @staticmethod
    def _after_changed(state: AgentState) -> str:
        if not state["changed"].items:
            return "report"
        if state["intent"].include_impacts:
            return "callers"
        if state["intent"].include_tests:
            return "tests"
        return "verify"

    @staticmethod
    def _after_callers(state: AgentState) -> str:
        return "tests" if state["intent"].include_tests else "verify"

    def build_graph(self):
        try:
            from langgraph.graph import END, START, StateGraph
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "LangGraph가 설치되지 않았습니다. Dev Container에서 'uv sync'를 실행하세요."
            ) from error

        builder = StateGraph(AgentState)
        builder.add_node("parse", self._parse_question)
        builder.add_node("changed", self._get_changed_functions)
        builder.add_node("callers", self._get_callers)
        builder.add_node("tests", self._find_tests)
        builder.add_node("verify", self._verify)
        builder.add_node("report", self._report)
        builder.add_edge(START, "parse")
        builder.add_conditional_edges(
            "parse",
            self._after_parse,
            {"changed": "changed", "report": "report"},
        )
        builder.add_conditional_edges(
            "changed",
            self._after_changed,
            {
                "callers": "callers",
                "tests": "tests",
                "verify": "verify",
                "report": "report",
            },
        )
        builder.add_conditional_edges(
            "callers",
            self._after_callers,
            {"tests": "tests", "verify": "verify"},
        )
        builder.add_edge("tests", "verify")
        builder.add_edge("verify", "report")
        builder.add_edge("report", END)
        return builder.compile()

    def run(self, question: str) -> AgentState:
        graph = self.build_graph()
        return graph.invoke({"question": question, "trace": []})

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from code_impact.agent.graph import CodeImpactAgent
from code_impact.agent.llm import (
    GeminiLanguageModel,
    GeminiResponseError,
    GroundedSummary,
    QuestionIntent,
)
from code_impact.demo_setup import create_demo_repository


class FakeGemini:
    model_name = "fake-gemini"

    def interpret_question(self, question: str) -> QuestionIntent:
        commit_ref = question.split(maxsplit=1)[0]
        return QuestionIntent(
            commit_ref=commit_ref,
            include_impacts="영향" in question,
            include_tests="테스트" in question,
        )

    def summarize_report(self, context: dict[str, Any]) -> GroundedSummary:
        return GroundedSummary(
            summary=(
                "변경 함수의 호출 관계를 최대 2단계까지 확인했습니다. "
                "영향 가능 함수와 연결된 테스트를 함께 검토하는 것이 좋습니다."
            ),
            evidence_ids=context["allowed_evidence_ids"],
        )


def test_safety_change_finds_two_hops_and_related_tests(tmp_path: Path) -> None:
    repo_path = tmp_path / "av-sim"
    commits = create_demo_repository(repo_path)
    agent = CodeImpactAgent(repo_path, tmp_path / "impact.db", FakeGemini())

    state = agent.run(f"{commits['safety_change'][:12]} 커밋의 영향 범위와 테스트를 알려줘.")

    changed_ids = {item.function.function_id for item in state["changed"].items}
    impact_depths = {item.function.function_id: item.depth for item in state["impacts"].items}
    test_ids = {item.function.function_id for item in state["tests"].items}

    assert changed_ids == {"control.safety_check::minimum_safe_distance"}
    assert impact_depths == {
        "control.safety_check::is_path_safe": 1,
        "control.controller::build_control_command": 2,
    }
    assert test_ids == {
        "tests.test_decision::test_controller_brakes_for_unsafe_path",
        "tests.test_safety::test_minimum_safe_distance_has_lower_bound",
        "tests.test_safety::test_path_is_unsafe_inside_minimum_distance",
    }
    assert state["evidence"]
    assert all(item.valid for item in state["evidence"])
    assert "## 분석 요약" in state["report"]
    assert "Gemini 질문 해석" in state["trace"][0]


def test_docs_only_commit_stops_without_guessing(tmp_path: Path) -> None:
    repo_path = tmp_path / "av-sim"
    create_demo_repository(repo_path)
    agent = CodeImpactAgent(repo_path, tmp_path / "impact.db", FakeGemini())

    state = agent.run("demo-docs-only 커밋의 영향 범위와 테스트를 알려줘.")

    assert state["changed"].items == []
    assert "영향 함수와 테스트를 추측하지 않습니다" in state["report"]


def test_gemini_intent_can_skip_unrequested_tools(tmp_path: Path) -> None:
    repo_path = tmp_path / "av-sim"
    create_demo_repository(repo_path)
    agent = CodeImpactAgent(repo_path, tmp_path / "impact.db", FakeGemini())

    state = agent.run("demo-safety-change 커밋에서 변경 함수만 알려줘.")

    assert "impacts" not in state
    assert "tests" not in state
    assert all("get_callers" not in step for step in state["trace"])
    assert all("find_related_tests" not in step for step in state["trace"])
    assert state["evidence"]
    assert all(item.evidence_id.startswith("CHG-") for item in state["evidence"])


def test_gemini_adapter_requests_structured_output() -> None:
    calls: list[dict[str, Any]] = []
    responses = [
        ('{"commit_ref":"demo-safety-change","include_impacts":true,"include_tests":true}'),
        '{"summary":"근거로 확인된 변경입니다.","evidence_ids":["CHG-001"]}',
    ]

    class FakeInteractions:
        def create(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(output_text=responses.pop(0))

    client = SimpleNamespace(interactions=FakeInteractions())
    language_model = GeminiLanguageModel(client, model_name="test-model")

    intent = language_model.interpret_question(
        "demo-safety-change 커밋의 영향 범위와 테스트를 알려줘."
    )
    summary = language_model.summarize_report(
        {
            "allowed_evidence_ids": ["CHG-001"],
            "changed_functions": [],
            "impact_functions": [],
            "recommended_tests": [],
            "evidence": [],
        }
    )

    assert intent.commit_ref == "demo-safety-change"
    assert summary.evidence_ids == ["CHG-001"]
    assert calls[0]["model"] == "test-model"
    assert calls[0]["response_format"]["mime_type"] == "application/json"
    assert "properties" in calls[0]["response_format"]["schema"]


def test_gemini_adapter_rejects_unknown_evidence_id() -> None:
    response = '{"summary":"확인되지 않은 근거입니다.","evidence_ids":["UNKNOWN-001"]}'

    class FakeInteractions:
        def create(self, **kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(output_text=response)

    client = SimpleNamespace(interactions=FakeInteractions())
    language_model = GeminiLanguageModel(client, model_name="test-model")

    with pytest.raises(GeminiResponseError, match="발급하지 않은 근거 ID"):
        language_model.summarize_report(
            {
                "allowed_evidence_ids": ["CHG-001"],
                "changed_functions": [],
                "impact_functions": [],
                "recommended_tests": [],
                "evidence": [],
            }
        )

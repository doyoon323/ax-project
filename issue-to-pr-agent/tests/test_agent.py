from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from issue_to_pr_agent.agent import AgentBudgetError, IssueFixAgent
from issue_to_pr_agent.config import Settings
from issue_to_pr_agent.models import IssueTask
from issue_to_pr_agent.tools import WorkspaceTools


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        gemini_api_key="test-key",
        github_token="test-token",
        github_webhook_secret="test-secret",
        github_repository="owner/repository",
        workspace_path=tmp_path,
        state_db_path=tmp_path / "jobs.sqlite3",
        worktree_root=tmp_path.parent / "worktrees",
        fetch_before_run=False,
        require_fail_to_pass=False,
    )


def response(payload: dict[str, Any]) -> SimpleNamespace:
    message = SimpleNamespace(content=json.dumps(payload))
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def write_regression_test(root: Path) -> None:
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_regression.py").write_text(
        "import unittest\n\n"
        "class RegressionTest(unittest.TestCase):\n"
        "    def test_smoke(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )


def test_fixed_loop_runs_three_phases(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    write_regression_test(tmp_path)
    subprocess.run(["git", "add", "sample.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)

    replies = iter(
        [
            response(
                {
                    "phase": "diagnose",
                    "note": "Locate the value.",
                    "commands": [["rg", "value", "sample.py"]],
                }
            ),
            response(
                {
                    "phase": "patch",
                    "note": "Apply the minimal fix.",
                    "edits": [{"path": "sample.py", "search": "value = 1", "replace": "value = 2"}],
                    "commands": [
                        ["python", "sample.py"],
                        ["python", "-m", "compileall", "sample.py"],
                    ],
                }
            ),
            response(
                {
                    "phase": "verify",
                    "note": "Review the final diff.",
                    "commands": [["git", "diff", "--", "sample.py"]],
                    "edits": [
                        {
                            "path": "sample.py",
                            "search": "value = 2",
                            "replace": "value = 999",
                        }
                    ],
                    "finish": True,
                    "summary": "Changed the sample value and compiled it.",
                    "pr_title": "fix: update sample value",
                    "pr_body": "Updates the value from one to two.",
                }
            ),
        ]
    )
    sleeps: list[float] = []
    completion_arguments: list[dict[str, Any]] = []

    class TransientProviderError(RuntimeError):
        status_code = 503

    def complete(**kwargs: Any) -> SimpleNamespace:
        completion_arguments.append(kwargs)
        if len(completion_arguments) == 1:
            raise TransientProviderError("temporary provider failure")
        return next(replies)

    agent = IssueFixAgent(
        make_settings(tmp_path),
        completion_fn=complete,
        sleep_fn=sleeps.append,
    )
    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=7,
        title="Update sample value",
        body="The expected value is two.",
        author="octocat",
        author_association="OWNER",
    )

    result = agent.run(issue, WorkspaceTools(tmp_path))

    assert result.success
    assert result.pr_title == "fix: update sample value"
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "value = 2\n"
    assert result.changed_paths == ["sample.py"]
    assert any("unittest" in item.argv for item in result.verification_results)
    assert result.total_tokens == 45
    assert result.prompt_tokens == 30
    assert result.completion_tokens == 15
    assert sleeps == [4.1, 4.1, 4.1, 4.1]
    assert [item["num_retries"] for item in completion_arguments] == [0, 0, 0, 0]


def test_verified_no_change_returns_success(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    write_regression_test(tmp_path)
    replies = iter(
        [
            response(
                {
                    "phase": "diagnose",
                    "note": "The requested behavior already exists.",
                    "commands": [["rg", "value", "sample.py"]],
                }
            ),
            response(
                {
                    "phase": "patch",
                    "note": "No edit is required.",
                    "commands": [["python", "-m", "compileall", "sample.py"]],
                }
            ),
            response(
                {
                    "phase": "verify",
                    "note": "Verified the existing implementation.",
                    "commands": [["git", "diff", "--", "sample.py"]],
                    "finish": True,
                    "summary": "The requested behavior is already implemented.",
                }
            ),
        ]
    )
    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=8,
        title="Keep the existing value",
        body="The value should be one.",
        author="octocat",
        author_association="OWNER",
    )

    result = IssueFixAgent(
        make_settings(tmp_path),
        completion_fn=lambda **_: next(replies),
        sleep_fn=lambda _: None,
    ).run(issue, WorkspaceTools(tmp_path))

    assert result.success
    assert result.changed_paths == []


def test_rate_limit_switches_to_sticky_groq_fallback(tmp_path: Path) -> None:
    settings = make_settings(tmp_path).model_copy(
        update={
            "llm_fallback_model": "groq/openai/gpt-oss-120b",
            "groq_api_key": SecretStr("groq-test-key"),
        }
    )
    calls: list[dict[str, Any]] = []
    fallback_response = object()

    class QuotaExceeded(RuntimeError):
        status_code = 429

    def complete(**kwargs: Any) -> object:
        calls.append(kwargs)
        if kwargs["model"].startswith("gemini/"):
            raise QuotaExceeded("daily quota exhausted")
        return fallback_response

    agent = IssueFixAgent(settings, completion_fn=complete, sleep_fn=lambda _: None)
    primary_arguments = {
        "model": settings.llm_model,
        "messages": [],
        "api_key": "gemini-test-key",
        "response_format": {"type": "json_object"},
        "num_retries": 0,
    }

    assert agent._complete_with_fallback(primary_arguments) is fallback_response
    assert agent._active_model == "groq/openai/gpt-oss-120b"
    assert calls[1]["api_key"] == "groq-test-key"
    assert calls[1]["response_format"]["type"] == "json_schema"

    fallback_arguments = {
        "model": agent._active_model,
        "messages": [],
        "api_key": "groq-test-key",
        "num_retries": 0,
    }
    assert agent._complete_with_fallback(fallback_arguments) is fallback_response
    assert [call["model"] for call in calls] == [
        "gemini/gemini-3.1-pro-preview",
        "groq/openai/gpt-oss-120b",
        "groq/openai/gpt-oss-120b",
    ]


def test_groq_schema_requires_all_fields_and_rejects_unknown_properties() -> None:
    response_format = IssueFixAgent._response_format_for_model("groq/openai/gpt-oss-120b")
    schema = response_format["json_schema"]["schema"]

    assert response_format["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["edits"]["items"]["additionalProperties"] is False
    assert "mode" in schema["properties"]["edits"]["items"]["required"]


def test_system_prompt_disables_provider_native_and_shell_tools() -> None:
    prompt = IssueFixAgent._system_prompt()

    assert "No native tools or functions are available" in prompt
    assert "Never use bash, sh, zsh" in prompt


def test_groq_json_generation_failure_is_retried(tmp_path: Path) -> None:
    settings = make_settings(tmp_path).model_copy(
        update={
            "llm_model": "groq/openai/gpt-oss-120b",
            "groq_api_key": SecretStr("groq-test-key"),
        }
    )
    calls = 0
    expected = object()

    class JsonGenerationError(RuntimeError):
        status_code = 400

    def complete(**_: Any) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise JsonGenerationError("json_validate_failed")
        return expected

    sleeps: list[float] = []
    agent = IssueFixAgent(settings, completion_fn=complete, sleep_fn=sleeps.append)

    assert agent._complete_with_transient_retries({"model": settings.llm_model}) is expected
    assert calls == 2
    assert sleeps == [4.1]


def test_groq_rate_limit_is_retried_without_retrying_gemini_quota(tmp_path: Path) -> None:
    calls = 0
    expected = object()

    class RateLimited(RuntimeError):
        status_code = 429

    def complete(**_: Any) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimited("try again shortly")
        return expected

    agent = IssueFixAgent(make_settings(tmp_path), completion_fn=complete, sleep_fn=lambda _: None)

    assert agent._complete_with_transient_retries({"model": "groq/openai/gpt-oss-120b"}) is expected
    assert calls == 2

    calls = 0
    try:
        agent._complete_with_transient_retries({"model": "gemini/gemini-3.6-flash"})
    except RateLimited:
        pass
    else:
        raise AssertionError("Gemini quota errors must switch providers, not retry")
    assert calls == 1


def test_wrong_phase_is_corrected_before_tools_run(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    replies = iter(
        [
            response(
                {
                    "phase": "patch",
                    "note": "Wrong phase label.",
                    "commands": [],
                    "edits": [],
                    "finish": False,
                    "summary": "",
                    "pr_title": "",
                    "pr_body": "",
                }
            ),
            response(
                {
                    "phase": "diagnose",
                    "note": "Corrected phase label.",
                    "commands": [],
                    "edits": [],
                    "finish": False,
                    "summary": "",
                    "pr_title": "",
                    "pr_body": "",
                }
            ),
        ]
    )
    calls: list[dict[str, Any]] = []

    def complete(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return next(replies)

    agent = IssueFixAgent(make_settings(tmp_path), completion_fn=complete, sleep_fn=lambda _: None)
    _, _, decision = agent._execute_phase([], WorkspaceTools(tmp_path), [], "diagnose")

    assert decision.phase == "diagnose"
    assert len(calls) == 2
    assert any("CORRECTION REQUIRED" in message["content"] for message in calls[1]["messages"])


def test_failed_gate_gets_one_bounded_correction_cycle(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "import unittest\nfrom sample import value\n\n"
        "class ValueTest(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(value, 2)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    replies = iter(
        [
            response({"phase": "diagnose", "note": "inspect", "commands": []}),
            response(
                {
                    "phase": "patch",
                    "note": "first attempt",
                    "edits": [{"path": "sample.py", "search": "value = 1", "replace": "value = 0"}],
                    "commands": [],
                }
            ),
            response(
                {
                    "phase": "verify",
                    "note": "verify",
                    "commands": [],
                    "finish": True,
                    "summary": "first attempt",
                }
            ),
            response(
                {
                    "phase": "patch",
                    "note": "correct from evidence",
                    "edits": [{"path": "sample.py", "search": "value = 0", "replace": "value = 2"}],
                    "commands": [],
                }
            ),
            response(
                {
                    "phase": "verify",
                    "note": "corrected",
                    "commands": [],
                    "finish": True,
                    "summary": "corrected value",
                }
            ),
        ]
    )
    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=9,
        title="Set value to two",
        body="Regression test describes the expected result.",
        author="octocat",
        author_association="OWNER",
    )

    result = IssueFixAgent(
        make_settings(tmp_path),
        completion_fn=lambda **_: next(replies),
        sleep_fn=lambda _: None,
    ).run(issue, WorkspaceTools(tmp_path))

    assert result.correction_cycles == 1
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "value = 2\n"
    assert all(item.succeeded for item in result.verification_results)


def test_provider_usage_stops_at_token_budget(tmp_path: Path) -> None:
    settings = make_settings(tmp_path).model_copy(update={"max_total_tokens_per_job": 1_000})
    agent = IssueFixAgent(settings)
    over_budget = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=800, completion_tokens=201, total_tokens=1_001)
    )

    with pytest.raises(AgentBudgetError, match="token budget exceeded"):
        agent._record_usage(over_budget)

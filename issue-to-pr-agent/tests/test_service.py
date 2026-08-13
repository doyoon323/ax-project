from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

import issue_to_pr_agent.service as service_module
from issue_to_pr_agent.agent import AgentExecutionError
from issue_to_pr_agent.config import Settings
from issue_to_pr_agent.github_client import GitHubPublishError, PublishResult, WorktreeSession
from issue_to_pr_agent.models import AgentRunResult, CommandResult, IssueTask
from issue_to_pr_agent.service import IssueToPRService


def make_settings(tmp_path: Path, *, publish_enabled: bool) -> Settings:
    return Settings(
        gemini_api_key="test-key",
        github_token="test-token",
        github_webhook_secret="test-secret",
        github_repository="owner/repository",
        workspace_path=tmp_path,
        worktree_root=tmp_path.parent / "worktrees",
        state_db_path=tmp_path / "jobs.sqlite3",
        fetch_before_run=False,
        verification_backend="host",
        allow_host_verification=True,
        publish_enabled=publish_enabled,
        github_expected_login="issue-agent-bot" if publish_enabled else "",
    )


def make_issue() -> IssueTask:
    return IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=42,
        title="Fix the value",
        body="The value must be two.",
        author="octocat",
        author_association="OWNER",
    )


def make_result(workspace: Path) -> AgentRunResult:
    return AgentRunResult(
        success=True,
        summary="fixed and verified",
        pr_title="fix: correct value",
        pr_body="Corrects the value and adds a regression test.",
        verification_results=[
            CommandResult(
                argv=("python", "-m", "unittest"),
                return_code=0,
                output="OK",
                is_verification=True,
            )
        ],
        baseline_verification_results=[
            CommandResult(
                argv=("python", "-m", "unittest"),
                return_code=1,
                output="failed as expected",
                is_verification=True,
            )
        ],
        changed_paths=["sample.py", "tests/test_sample.py"],
        model_history=["gemini/test"],
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        estimated_cost_usd=0.001,
        correction_cycles=1,
        duration_seconds=2.5,
        workspace=workspace,
    )


def build_service(
    monkeypatch: object,
    settings: Settings,
    workspace: Path,
) -> tuple[IssueToPRService, Mock, Mock]:
    workspaces = Mock()
    workspaces.prepare.return_value = WorktreeSession(
        path=workspace,
        branch="agent/issue-42-abcdef12",
    )
    workspaces.commit_and_push.return_value = ["sample.py", "tests/test_sample.py"]
    workspaces.current_head.return_value = "a" * 40
    github = Mock()
    github.publish_draft_pr.return_value = PublishResult(
        pr_number=7,
        pr_url="https://github.com/owner/repository/pull/7",
        assignee_added=True,
    )
    agent = Mock()
    agent.run.return_value = make_result(workspace)

    monkeypatch.setattr(service_module, "GitWorkspaceManager", lambda _: workspaces)
    monkeypatch.setattr(service_module, "GitHubClient", lambda _: github)
    monkeypatch.setattr(service_module, "IssueFixAgent", lambda *_args, **_kwargs: agent)
    return IssueToPRService(settings), workspaces, github


def test_service_dry_run_never_publishes(monkeypatch: object, tmp_path: Path) -> None:
    service, workspaces, github = build_service(
        monkeypatch,
        make_settings(tmp_path, publish_enabled=False),
        tmp_path,
    )

    result = service.process(make_issue())

    assert result["status"] == "dry-run"
    assert result["fail_to_pass_proven"] is True
    assert result["estimated_cost_usd"] == 0.001
    github.publish_draft_pr.assert_not_called()
    workspaces.commit_and_push.assert_not_called()
    workspaces.cleanup.assert_called_once()


def test_service_rejects_success_without_actual_changes(
    monkeypatch: object, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, publish_enabled=False)
    service, workspaces, github = build_service(monkeypatch, settings, tmp_path)
    empty_result = make_result(tmp_path)
    empty_result.changed_paths = []
    agent = Mock()
    agent.run.return_value = empty_result
    monkeypatch.setattr(service_module, "IssueFixAgent", lambda *_args, **_kwargs: agent)

    with pytest.raises(AgentExecutionError, match="no verified file changes"):
        service.process(make_issue())

    workspaces.commit_and_push.assert_not_called()
    github.publish_draft_pr.assert_not_called()
    workspaces.cleanup.assert_called_once()


def test_service_publishes_only_after_agent_success(monkeypatch: object, tmp_path: Path) -> None:
    service, workspaces, github = build_service(
        monkeypatch,
        make_settings(tmp_path, publish_enabled=True),
        tmp_path,
    )

    result = service.process(make_issue())

    assert result["status"] == "published"
    assert result["pr_number"] == 7
    assert result["changed_files"] == ["sample.py", "tests/test_sample.py"]
    github.validate_identity.assert_called_once()
    workspaces.commit_and_push.assert_called_once()
    github.publish_draft_pr.assert_called_once()
    github.upsert_verification_check.assert_called_once()
    workspaces.cleanup.assert_called_once()


def test_service_requires_github_check_before_draft_pr(monkeypatch: object, tmp_path: Path) -> None:
    service, workspaces, github = build_service(
        monkeypatch,
        make_settings(tmp_path, publish_enabled=True),
        tmp_path,
    )
    github.upsert_verification_check.side_effect = GitHubPublishError(
        "checks permission denied",
        status_code=403,
    )

    with pytest.raises(GitHubPublishError, match="checks permission denied"):
        service.process(make_issue())

    workspaces.commit_and_push.assert_called_once()
    github.publish_draft_pr.assert_not_called()
    workspaces.cleanup.assert_called_once()


def test_service_resumes_publication_without_rerunning_agent(
    monkeypatch: object, tmp_path: Path
) -> None:
    service, workspaces, github = build_service(
        monkeypatch,
        make_settings(tmp_path, publish_enabled=True),
        tmp_path,
    )
    github.publish_draft_pr.side_effect = [
        GitHubPublishError("temporary ssl failure", retryable=True),
        PublishResult(
            pr_number=7,
            pr_url="https://github.com/owner/repository/pull/7",
            assignee_added=True,
        ),
    ]
    issue = make_issue()

    with pytest.raises(GitHubPublishError, match="temporary ssl failure"):
        service.process(issue)

    checkpoint = service.usage_store.checkpoint(issue.delivery_id)
    assert checkpoint is not None
    assert checkpoint["kind"] == "publication-pending"

    result = service.process(issue)

    assert result["status"] == "published"
    assert workspaces.prepare.call_count == 1
    assert workspaces.commit_and_push.call_count == 1
    assert github.publish_draft_pr.call_count == 2

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .agent import IssueFixAgent
from .config import Settings
from .github_client import (
    GitHubClient,
    GitHubPublishError,
    GitWorkspaceManager,
    WorktreeSession,
)
from .models import IssueTask
from .tools import WorkspaceTools


class IssueToPRService:
    """Coordinates isolated analysis, fixed publication logic, and cleanup."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workspaces = GitWorkspaceManager(settings)
        self.github = GitHubClient(settings)
        if settings.publish_enabled:
            self.github.validate_identity()

    def process(self, issue: IssueTask) -> dict[str, Any]:
        session: WorktreeSession | None = None
        succeeded = False
        try:
            session = self.workspaces.prepare(issue)
            tools = WorkspaceTools(
                session.path,
                timeout_seconds=self.settings.command_timeout_seconds,
                verification_timeout_seconds=self.settings.verification_timeout_seconds,
                max_output_chars=self.settings.max_output_chars,
                max_output_lines=self.settings.max_output_lines,
                verification_backend=self.settings.verification_backend,
                verification_container_image=self.settings.verification_container_image,
                verification_runner_queue_path=self.settings.verification_runner_queue_path,
                runner_worktree_root=self.settings.worktree_root,
                verification_runner_poll_seconds=self.settings.verification_runner_poll_seconds,
            )
            agent_result = IssueFixAgent(self.settings).run(issue, tools)
            run_metadata = {
                "models": agent_result.model_history,
                "usage": {
                    "prompt_tokens": agent_result.prompt_tokens,
                    "completion_tokens": agent_result.completion_tokens,
                    "total_tokens": agent_result.total_tokens,
                },
                "estimated_cost_usd": agent_result.estimated_cost_usd,
                "correction_cycles": agent_result.correction_cycles,
                "duration_seconds": agent_result.duration_seconds,
                "fail_to_pass_proven": bool(agent_result.baseline_verification_results),
            }

            if not agent_result.changed_paths:
                warning = ""
                if self.settings.publish_enabled:
                    try:
                        self.github.comment_on_issue(
                            issue.number,
                            "Issue-to-PR Agent가 현재 `main`과 검증 결과를 확인했지만 "
                            "필요한 코드 변경을 찾지 못했습니다. Draft PR은 생성하지 않았습니다.",
                        )
                    except GitHubPublishError:
                        warning = "No-change result was detected, but the issue comment failed."
                succeeded = True
                return {
                    "status": "no-change",
                    "summary": agent_result.summary,
                    "warning": warning,
                    **run_metadata,
                }

            if not self.settings.publish_enabled:
                succeeded = True
                return {
                    "status": "dry-run",
                    "summary": agent_result.summary,
                    "branch": session.branch,
                    "changed_files": agent_result.changed_paths,
                    **run_metadata,
                }

            changed_files = self.workspaces.commit_and_push(
                session,
                issue.number,
                agent_result.changed_paths,
            )
            publish_result = self.github.publish_draft_pr(issue, session.branch, agent_result)
            check_warning = ""
            if self.settings.github_checks_enabled:
                try:
                    self.github.upsert_verification_check(
                        issue,
                        self.workspaces.current_head(session),
                        agent_result,
                    )
                except GitHubPublishError:
                    check_warning = "GitHub Check를 기록하지 못했지만 Draft PR은 생성했습니다."
            succeeded = True
            published = asdict(publish_result)
            published["warning"] = " ".join(
                item for item in (publish_result.warning, check_warning) if item
            )
            return {
                "status": "published",
                "changed_files": changed_files,
                **run_metadata,
                **published,
            }
        finally:
            if session is not None and (succeeded or not self.settings.keep_failed_worktree):
                self.workspaces.cleanup(session)

    def report_status(
        self,
        issue: IssueTask,
        status: str,
        attempt: int,
        max_attempts: int,
        detail: str,
    ) -> None:
        if not self.settings.publish_enabled:
            return
        self.github.upsert_status_comment(
            issue.number,
            status=status,
            attempt=attempt,
            max_attempts=max_attempts,
            detail=detail,
        )

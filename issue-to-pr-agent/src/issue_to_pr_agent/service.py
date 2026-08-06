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

    def process(self, issue: IssueTask) -> dict[str, Any]:
        session: WorktreeSession | None = None
        succeeded = False
        try:
            session = self.workspaces.prepare(issue)
            tools = WorkspaceTools(
                session.path,
                timeout_seconds=self.settings.command_timeout_seconds,
                max_output_chars=self.settings.max_output_chars,
                max_output_lines=self.settings.max_output_lines,
            )
            agent_result = IssueFixAgent(self.settings).run(issue, tools)

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
                }

            if not self.settings.publish_enabled:
                succeeded = True
                return {
                    "status": "dry-run",
                    "summary": agent_result.summary,
                    "branch": session.branch,
                    "changed_files": agent_result.changed_paths,
                }

            changed_files = self.workspaces.commit_and_push(
                session,
                issue.number,
                agent_result.changed_paths,
            )
            publish_result = self.github.publish_draft_pr(issue, session.branch, agent_result)
            succeeded = True
            return {
                "status": "published",
                "changed_files": changed_files,
                **asdict(publish_result),
            }
        finally:
            if session is not None and (succeeded or not self.settings.keep_failed_worktree):
                self.workspaces.cleanup(session)

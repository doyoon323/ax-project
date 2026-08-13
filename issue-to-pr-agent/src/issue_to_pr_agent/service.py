from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .agent import AgentExecutionError, IssueFixAgent
from .config import Settings
from .github_client import (
    GitHubClient,
    GitWorkspaceManager,
    WorktreeSession,
)
from .jobs import JobStore
from .models import AgentRunResult, CommandResult, IssueTask
from .tools import WorkspaceTools


class IssueToPRService:
    """Coordinates isolated analysis, fixed publication logic, and cleanup."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workspaces = GitWorkspaceManager(settings)
        self.github = GitHubClient(settings)
        self.usage_store = JobStore(settings.state_db_path)
        self.usage_store.initialize()
        if settings.publish_enabled:
            self.github.validate_identity()

    def process(self, issue: IssueTask) -> dict[str, Any]:
        session: WorktreeSession | None = None
        succeeded = False
        try:
            self.usage_store.enqueue(issue)
            checkpoint = self.usage_store.checkpoint(issue.delivery_id)
            if checkpoint is not None and checkpoint.get("kind") == "publication-pending":
                return self._resume_publication(issue, checkpoint)

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
            usage = self.usage_store.usage(issue.delivery_id)
            agent_result = IssueFixAgent(
                self.settings,
                usage_callback=(
                    lambda prompt, completion, total, cost: self.usage_store.record_usage(
                        issue.delivery_id,
                        prompt_tokens=prompt,
                        completion_tokens=completion,
                        total_tokens=total,
                        estimated_cost_usd=cost,
                    )
                ),
                initial_prompt_tokens=usage.prompt_tokens,
                initial_completion_tokens=usage.completion_tokens,
                initial_total_tokens=usage.total_tokens,
                initial_estimated_cost_usd=usage.estimated_cost_usd,
            ).run(issue, tools)
            run_metadata = self._run_metadata(agent_result)

            if not agent_result.changed_paths:
                raise AgentExecutionError(
                    "agent produced no verified file changes; "
                    "refusing a successful no-change result"
                )

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
            checkpoint = {
                "kind": "publication-pending",
                "branch": session.branch,
                "head_sha": self.workspaces.current_head(session),
                "changed_files": changed_files,
                "agent_result": self._serialize_agent_result(agent_result),
            }
            self.usage_store.save_checkpoint(issue.delivery_id, checkpoint)
            result = self._resume_publication(issue, checkpoint)
            succeeded = True
            return result
        finally:
            if session is not None and (succeeded or not self.settings.keep_failed_worktree):
                self.workspaces.cleanup(session)

    def _resume_publication(self, issue: IssueTask, checkpoint: dict[str, Any]) -> dict[str, Any]:
        branch = checkpoint.get("branch")
        head_sha = checkpoint.get("head_sha")
        changed_files = checkpoint.get("changed_files")
        raw_agent_result = checkpoint.get("agent_result")
        if not (
            isinstance(branch, str)
            and isinstance(head_sha, str)
            and isinstance(changed_files, list)
            and all(isinstance(path, str) for path in changed_files)
            and isinstance(raw_agent_result, dict)
        ):
            raise AgentExecutionError("publication checkpoint is invalid")
        agent_result = self._deserialize_agent_result(raw_agent_result)
        if self.settings.github_checks_enabled:
            self.github.upsert_verification_check(issue, head_sha, agent_result)
        publish_result = self.github.publish_draft_pr(issue, branch, agent_result)
        return {
            "status": "published",
            "changed_files": changed_files,
            **self._run_metadata(agent_result),
            **asdict(publish_result),
        }

    @staticmethod
    def _run_metadata(agent_result: AgentRunResult) -> dict[str, Any]:
        return {
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
            "localization": {
                "candidates": agent_result.localization_candidates,
                "scanned_files": agent_result.localization_scanned_files,
            },
        }

    @staticmethod
    def _serialize_agent_result(result: AgentRunResult) -> dict[str, Any]:
        value = asdict(result)
        value.pop("workspace", None)
        return value

    @staticmethod
    def _deserialize_agent_result(value: dict[str, Any]) -> AgentRunResult:
        data = dict(value)
        data["verification_results"] = [
            CommandResult(
                argv=tuple(item["argv"]), **{k: v for k, v in item.items() if k != "argv"}
            )
            for item in data.get("verification_results", [])
        ]
        data["baseline_verification_results"] = [
            CommandResult(
                argv=tuple(item["argv"]), **{k: v for k, v in item.items() if k != "argv"}
            )
            for item in data.get("baseline_verification_results", [])
        ]
        return AgentRunResult(**data)

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

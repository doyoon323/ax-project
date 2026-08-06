from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import Settings
from .models import AgentRunResult, IssueTask


class GitOperationError(RuntimeError):
    """Raised when a trusted Git lifecycle command fails."""


class GitHubPublishError(RuntimeError):
    """Raised when the GitHub API cannot complete publication."""


@dataclass(frozen=True)
class WorktreeSession:
    path: Path
    branch: str


@dataclass(frozen=True)
class PublishResult:
    pr_number: int
    pr_url: str
    assignee_added: bool
    warning: str = ""


class GitWorkspaceManager:
    """Creates and cleans an isolated worktree without touching the user's checkout."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repository_root = settings.workspace_path.resolve(strict=True)
        self.worktree_root = settings.worktree_root.resolve(strict=False)
        if self.worktree_root == Path("/") or self.worktree_root.is_relative_to(
            self.repository_root
        ):
            raise GitOperationError("WORKTREE_ROOT must be outside the target repository")
        self._validate_repository()

    def prepare(self, issue: IssueTask) -> WorktreeSession:
        delivery_suffix = re.sub(r"[^a-zA-Z0-9]", "", issue.delivery_id)[:8].lower()
        if len(delivery_suffix) < 6:
            raise GitOperationError("delivery id is too short to create an isolated branch")
        branch = f"agent/issue-{issue.number}-{delivery_suffix}"
        path = self.worktree_root / f"issue-{issue.number}-{delivery_suffix}"
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise GitOperationError(f"worktree path already exists: {path}")

        if self.settings.fetch_before_run:
            self._git(["fetch", "--no-tags", "origin", self.settings.base_branch])
        base_ref = f"refs/remotes/origin/{self.settings.base_branch}"
        self._git(["show-ref", "--verify", "--quiet", base_ref], allow_codes={0})
        self._git(["worktree", "add", "--detach", str(path), base_ref])
        try:
            self._git(["switch", "-c", branch], cwd=path)
        except Exception:
            self._git(["worktree", "remove", "--force", str(path)])
            raise
        return WorktreeSession(path=path, branch=branch)

    def commit_and_push(
        self,
        session: WorktreeSession,
        issue_number: int,
        edited_paths: list[str],
    ) -> list[str]:
        if not edited_paths:
            raise GitOperationError("agent did not report any edited paths")
        self._git(["add", "--", *edited_paths], cwd=session.path)
        raw = self._git(["diff", "--cached", "--name-only", "-z"], cwd=session.path)
        changed_files = [item for item in raw.split("\0") if item]
        if not changed_files:
            raise GitOperationError("no staged changes are available to publish")
        for path in changed_files:
            self._reject_sensitive_path(path)
        unstaged = self._git(["diff", "--name-only", "--"], cwd=session.path)
        if unstaged:
            raise GitOperationError(
                "verification modified tracked files outside the approved edits"
            )

        self._git(
            ["commit", "-m", f"fix: auto-resolve issue #{issue_number}"],
            cwd=session.path,
        )
        self._git(
            ["push", "--set-upstream", "origin", f"HEAD:refs/heads/{session.branch}"],
            cwd=session.path,
            timeout=120,
        )
        return changed_files

    def cleanup(self, session: WorktreeSession) -> None:
        if session.path.exists():
            self._git(["worktree", "remove", "--force", str(session.path)])
        self._git(["branch", "-D", session.branch], allow_codes={0, 1})

    def _validate_repository(self) -> None:
        root = self._git(["rev-parse", "--show-toplevel"], cwd=self.repository_root)
        if Path(root).resolve() != self.repository_root:
            raise GitOperationError("WORKSPACE_PATH must point to the target repository root")
        origin = self._git(["remote", "get-url", "origin"], cwd=self.repository_root)
        normalized = origin.removesuffix(".git").replace(":", "/")
        if not normalized.endswith(f"/{self.settings.github_repository}"):
            raise GitOperationError("origin remote does not match GITHUB_REPOSITORY")

    def _git(
        self,
        arguments: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = 60,
        allow_codes: set[int] | None = None,
    ) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=cwd or self.repository_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        accepted = allow_codes or {0}
        if completed.returncode not in accepted:
            message = (completed.stderr or completed.stdout).strip()[:500]
            raise GitOperationError(f"git {arguments[0]} failed: {message}")
        return completed.stdout.strip()

    @staticmethod
    def _reject_sensitive_path(raw_path: str) -> None:
        path = Path(raw_path)
        name = path.name.lower()
        lowered_path = Path(*(part.lower() for part in path.parts))
        if (
            ".git" in {part.lower() for part in path.parts}
            or name == ".env"
            or name.startswith(".env.")
            or name in {"credentials", "id_ed25519", "id_rsa"}
            or path.suffix.lower() in {".key", ".p12", ".pem", ".pfx"}
            or Path(".github/workflows") in lowered_path.parents
            or Path(".github/actions") in lowered_path.parents
        ):
            raise GitOperationError(f"refusing to publish sensitive path: {raw_path}")


class GitHubClient:
    """Minimal REST client for Draft PR creation, assignment, and issue comments."""

    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": settings.github_api_version,
            "User-Agent": "issue-to-pr-agent/0.1",
        }
        token = self._resolve_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.session.headers.update(headers)

    def list_candidate_issues(self) -> list[IssueTask]:
        """Return open labeled Issues eligible for the local polling worker."""

        params: dict[str, Any] = {
            "state": "open",
            "sort": "updated",
            "direction": "asc",
            "per_page": 100,
        }
        if self.settings.required_issue_label:
            params["labels"] = self.settings.required_issue_label
        payload = self._request(
            "GET",
            f"/repos/{self.settings.github_repository}/issues",
            params=params,
            expected={200},
        )
        return [issue for item in payload if (issue := self._to_issue_task(item)) is not None]

    def publish_draft_pr(
        self,
        issue: IssueTask,
        branch: str,
        result: AgentRunResult,
    ) -> PublishResult:
        existing = self._find_pull_request(branch)
        if existing is None:
            body = self._pull_request_body(issue, result)
            pull = self._request(
                "POST",
                f"/repos/{self.settings.github_repository}/pulls",
                json={
                    "title": result.pr_title,
                    "body": body,
                    "head": branch,
                    "base": self.settings.base_branch,
                    "draft": True,
                },
                expected={201},
            )
        else:
            pull = existing

        pr_number = int(pull["number"])
        pr_url = str(pull["html_url"])
        assignee_added, warning = self._try_assign(pr_number, issue.author)
        try:
            self.comment_on_issue(
                issue.number,
                f"Draft PR created by Issue-to-PR Agent: {pr_url}",
            )
        except GitHubPublishError:
            extra = "Draft PR was created, but the issue comment could not be posted."
            warning = f"{warning} {extra}".strip()
        return PublishResult(
            pr_number=pr_number,
            pr_url=pr_url,
            assignee_added=assignee_added,
            warning=warning,
        )

    def comment_on_issue(self, issue_number: int, body: str) -> None:
        self._request(
            "POST",
            f"/repos/{self.settings.github_repository}/issues/{issue_number}/comments",
            json={"body": body},
            expected={201},
        )

    def _find_pull_request(self, branch: str) -> dict[str, Any] | None:
        owner = self.settings.github_repository.split("/", 1)[0]
        pulls = self._request(
            "GET",
            f"/repos/{self.settings.github_repository}/pulls",
            params={"head": f"{owner}:{branch}", "state": "all", "per_page": 1},
            expected={200},
        )
        return pulls[0] if pulls else None

    def _try_assign(self, pr_number: int, author: str) -> tuple[bool, str]:
        try:
            check = self.session.get(
                self._url(f"/repos/{self.settings.github_repository}/assignees/{author}"),
                timeout=30,
            )
        except requests.RequestException:
            return False, "Draft PR was created, but assignability could not be checked."
        if check.status_code != 204:
            return False, f"{author} is not assignable in this repository."
        try:
            self._request(
                "POST",
                f"/repos/{self.settings.github_repository}/issues/{pr_number}/assignees",
                json={"assignees": [author]},
                expected={201},
            )
        except GitHubPublishError:
            return False, "Draft PR was created, but the assignee could not be added."
        return True, ""

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: set[int],
        **kwargs: Any,
    ) -> Any:
        try:
            response = self.session.request(method, self._url(path), timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise GitHubPublishError(f"GitHub API request failed: {type(exc).__name__}") from exc
        if response.status_code not in expected:
            request_id = response.headers.get("X-GitHub-Request-Id", "unknown")
            raise GitHubPublishError(
                f"GitHub API returned {response.status_code}; request id={request_id}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubPublishError("GitHub API returned invalid JSON") from exc

    def _url(self, path: str) -> str:
        return f"{self.settings.github_api_url.rstrip('/')}{path}"

    def _resolve_token(self) -> str:
        configured = self.settings.github_token.get_secret_value()
        if configured:
            return configured
        try:
            completed = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return ""
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def _to_issue_task(self, payload: dict[str, Any]) -> IssueTask | None:
        if "pull_request" in payload:
            return None
        association = str(payload.get("author_association") or "").upper()
        if association not in self.settings.allowed_associations:
            return None
        labels = {
            str(item.get("name"))
            for item in payload.get("labels", [])
            if isinstance(item, dict) and item.get("name")
        }
        required_label = self.settings.required_issue_label
        if required_label and required_label not in labels:
            return None

        user = payload.get("user") or {}
        number = payload.get("number")
        title = payload.get("title")
        author = user.get("login")
        if not all(
            (
                isinstance(number, int),
                isinstance(title, str),
                isinstance(author, str),
            )
        ):
            return None
        body = payload.get("body")
        if body is not None and not isinstance(body, str):
            return None

        fingerprint = f"{self.settings.github_repository}:{number}".encode()
        delivery_id = f"poll-{hashlib.sha256(fingerprint).hexdigest()[:32]}"
        return IssueTask(
            delivery_id=delivery_id,
            repository=self.settings.github_repository,
            number=number,
            title=title,
            body=body or "",
            author=author,
            author_association=association,
        )

    @staticmethod
    def _pull_request_body(issue: IssueTask, result: AgentRunResult) -> str:
        verification = "\n".join(
            f"- `{' '.join(item.argv)}`: {'passed' if item.succeeded else 'failed'}"
            for item in result.verification_results
        )
        details = result.pr_body.strip() or result.summary
        return (
            f"{details}\n\n"
            "### Verification\n"
            f"{verification or '- No verification recorded'}\n\n"
            f"Closes #{issue.number}\n\n"
            "_Generated as a Draft PR by Issue-to-PR Agent. Human review is required._"
        )

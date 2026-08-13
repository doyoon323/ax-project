from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .config import Settings
from .models import AgentRunResult, IssueTask


class GitOperationError(RuntimeError):
    """Raised when a trusted Git lifecycle command fails."""


class GitHubPublishError(RuntimeError):
    """Raised when the GitHub API cannot complete publication."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


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
        source_root = settings.workspace_path.resolve(strict=True)
        self.repository_root = source_root
        self.worktree_root = settings.worktree_root.resolve(strict=False)
        if self.worktree_root == Path("/") or self.worktree_root.is_relative_to(
            self.repository_root
        ):
            raise GitOperationError("WORKTREE_ROOT must be outside the target repository")
        self._validate_repository()
        if settings.repository_mirror_path is not None:
            self._prepare_repository_mirror(source_root, settings.repository_mirror_path)

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
            [
                "-c",
                f"user.name={self.settings.git_author_name}",
                "-c",
                f"user.email={self.settings.git_author_email}",
                "commit",
                "-m",
                f"fix: auto-resolve issue #{issue_number}",
            ],
            cwd=session.path,
        )
        remote_ref = f"refs/heads/{session.branch}"
        existing = self._git(
            ["ls-remote", "--heads", "origin", remote_ref],
            cwd=session.path,
        )
        push_arguments = ["push", "--set-upstream"]
        if existing:
            remote_sha = existing.split(maxsplit=1)[0]
            if re.fullmatch(r"[0-9a-f]{40,64}", remote_sha) is None:
                raise GitOperationError("remote agent branch returned an invalid commit id")
            push_arguments.append(f"--force-with-lease={remote_ref}:{remote_sha}")
        push_arguments.extend(["origin", f"HEAD:{remote_ref}"])
        self._git(
            push_arguments,
            cwd=session.path,
            timeout=120,
        )
        return changed_files

    def current_head(self, session: WorktreeSession) -> str:
        head = self._git(["rev-parse", "HEAD"], cwd=session.path)
        if re.fullmatch(r"[0-9a-f]{40,64}", head) is None:
            raise GitOperationError("published worktree has an invalid commit id")
        return head

    def cleanup(self, session: WorktreeSession) -> None:
        if session.path.exists():
            self._git(["worktree", "remove", "--force", str(session.path)])
        self._git(["branch", "-D", session.branch], allow_codes={0, 1})

    def _validate_repository(self) -> None:
        root = self._git(["rev-parse", "--show-toplevel"], cwd=self.repository_root)
        if Path(root).resolve() != self.repository_root:
            raise GitOperationError("WORKSPACE_PATH must point to the target repository root")
        origin = self._git(["remote", "get-url", "origin"], cwd=self.repository_root)
        if self._is_local_origin(origin):
            if not self.settings.allow_local_git_origin:
                raise GitOperationError("local Git origins are disabled")
            return
        if not self._matches_github_origin(origin):
            raise GitOperationError("origin remote does not match GITHUB_REPOSITORY")

    def _prepare_repository_mirror(self, source_root: Path, raw_mirror_path: Path) -> None:
        mirror_path = raw_mirror_path.resolve(strict=False)
        if (
            mirror_path == Path("/")
            or mirror_path == source_root
            or mirror_path.is_relative_to(source_root)
            or self.worktree_root.is_relative_to(mirror_path)
            or mirror_path.is_relative_to(self.worktree_root)
        ):
            raise GitOperationError(
                "REPOSITORY_MIRROR_PATH must be separate from the source and worktree roots"
            )
        source_origin = self._git(["remote", "get-url", "origin"], cwd=source_root)
        if not mirror_path.exists():
            mirror_path.parent.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    "--no-hardlinks",
                    str(source_root),
                    str(mirror_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout).strip()[:500]
                raise GitOperationError(f"git clone for repository mirror failed: {message}")
        self.repository_root = mirror_path.resolve(strict=True)
        self._git(["remote", "set-url", "origin", source_origin])
        self._validate_repository()

    @staticmethod
    def _is_local_origin(origin: str) -> bool:
        return origin.startswith(("/", "./", "../", "file://"))

    def _matches_github_origin(self, origin: str) -> bool:
        expected_path = f"/{self.settings.github_repository}"
        if origin.startswith("git@github.com:"):
            actual_path = "/" + origin.removeprefix("git@github.com:").removesuffix(".git")
            return actual_path == expected_path
        parsed = urlparse(origin)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.username is None
            and parsed.password is None
            and parsed.path.removesuffix(".git") == expected_path
        )

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
        self.identity_login = ""

    def validate_identity(self) -> str:
        """Fail closed before publication when the token belongs to an unexpected account."""

        payload = self._request("GET", "/user", expected={200})
        login = str((payload or {}).get("login") or "")
        expected = self.settings.github_expected_login
        if not login or login.casefold() != expected.casefold():
            raise GitHubPublishError(
                f"GitHub token identity mismatch; expected {expected!r}, received {login!r}"
            )
        self.identity_login = login
        return login

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

    def upsert_status_comment(
        self,
        issue_number: int,
        *,
        status: str,
        attempt: int,
        max_attempts: int,
        detail: str,
    ) -> None:
        marker = "<!-- issue-to-pr-agent-status -->"
        comments = self._request(
            "GET",
            f"/repos/{self.settings.github_repository}/issues/{issue_number}/comments",
            params={"per_page": 100},
            expected={200},
        )
        existing = next(
            (
                item
                for item in comments
                if isinstance(item, dict) and marker in str(item.get("body") or "")
            ),
            None,
        )
        body = self._status_comment_body(
            status=status,
            attempt=attempt,
            max_attempts=max_attempts,
            detail=detail,
            actor=self.identity_login or self.settings.github_expected_login,
        )
        if existing is None:
            self.comment_on_issue(issue_number, body)
            return
        comment_id = existing.get("id")
        if not isinstance(comment_id, int):
            raise GitHubPublishError("existing Agent status comment has no numeric id")
        self._request(
            "PATCH",
            f"/repos/{self.settings.github_repository}/issues/comments/{comment_id}",
            json={"body": body},
            expected={200},
        )

    def upsert_verification_check(
        self,
        issue: IssueTask,
        head_sha: str,
        result: AgentRunResult,
    ) -> None:
        name = "Issue-to-PR Agent / verification"
        payload = {
            "name": name,
            "head_sha": head_sha,
            "external_id": issue.delivery_id,
            "status": "completed",
            "conclusion": "success",
            "output": {
                "title": "Bounded verification passed",
                "summary": self._check_summary(result),
            },
        }
        existing = self._request(
            "GET",
            f"/repos/{self.settings.github_repository}/commits/{head_sha}/check-runs",
            params={"check_name": name, "filter": "latest", "per_page": 100},
            expected={200},
        )
        check_runs = (existing or {}).get("check_runs", [])
        match = next(
            (
                item
                for item in check_runs
                if isinstance(item, dict) and item.get("external_id") == issue.delivery_id
            ),
            None,
        )
        if match is None:
            self._request(
                "POST",
                f"/repos/{self.settings.github_repository}/check-runs",
                json=payload,
                expected={201},
            )
            return
        check_id = match.get("id")
        if not isinstance(check_id, int):
            raise GitHubPublishError("existing verification check has no numeric id")
        update = dict(payload)
        update.pop("head_sha")
        self._request(
            "PATCH",
            f"/repos/{self.settings.github_repository}/check-runs/{check_id}",
            json=update,
            expected={200},
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
            raise GitHubPublishError(
                f"GitHub API request failed: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code not in expected:
            request_id = response.headers.get("X-GitHub-Request-Id", "unknown")
            raise GitHubPublishError(
                f"GitHub API returned {response.status_code}; request id={request_id}",
                status_code=response.status_code,
                retryable=response.status_code == 429 or response.status_code >= 500,
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

        # Comments (including our status comment) do not affect this revision key.
        # Editing the Issue title/body creates a deliberate new poll job after a final failure.
        fingerprint = (f"{self.settings.github_repository}:{number}:{title}:{body or ''}").encode()
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
        models = ", ".join(f"`{model}`" for model in result.model_history) or "not recorded"
        usage = (
            f"{result.total_tokens} "
            f"(input {result.prompt_tokens}, output {result.completion_tokens})"
            if result.total_tokens
            else "not reported by provider"
        )
        baseline = (
            "failed as expected"
            if result.baseline_verification_results
            and any(not item.succeeded for item in result.baseline_verification_results)
            else "not recorded"
        )
        return (
            f"{details}\n\n"
            "### Verification\n"
            f"{verification or '- No verification recorded'}\n\n"
            "### Agent run\n"
            f"- Models: {models}\n"
            f"- Recorded tokens: {usage}\n"
            f"- Estimated cost: `${result.estimated_cost_usd:.4f}`\n"
            f"- Correction cycles: {result.correction_cycles}\n"
            f"- Fail-to-pass baseline: {baseline}\n"
            f"- Duration: {result.duration_seconds:.3f}s\n\n"
            f"Closes #{issue.number}\n\n"
            "_Generated as a Draft PR by Issue-to-PR Agent. Human review is required._"
        )

    @staticmethod
    def _check_summary(result: AgentRunResult) -> str:
        commands = "\n".join(
            f"- `{' '.join(item.argv)}`: {'passed' if item.succeeded else 'failed'}"
            for item in result.verification_results
        )
        return (
            f"{commands or '- No commands recorded'}\n\n"
            "Fail-to-pass: "
            f"{'passed' if result.baseline_verification_results else 'not recorded'}; "
            f"corrections: {result.correction_cycles}; "
            f"tokens: {result.total_tokens}; "
            f"estimated cost: ${result.estimated_cost_usd:.4f}."
        )[:65_000]

    @staticmethod
    def _status_comment_body(
        *,
        status: str,
        attempt: int,
        max_attempts: int,
        detail: str,
        actor: str,
    ) -> str:
        labels = {
            "running": "진행 중",
            "retrying": "재시도 대기",
            "completed": "완료",
            "failed": "실패",
        }
        safe_detail = re.sub(r"[\r\n]+", " ", detail).strip()[:300]
        safe_actor = re.sub(r"[^A-Za-z0-9-]", "", actor)[:39] or "unknown"
        return (
            "<!-- issue-to-pr-agent-status -->\n"
            "### Issue-to-PR Agent 상태\n"
            f"- 상태: **{labels.get(status, status)}**\n"
            f"- 시도: {attempt}/{max_attempts}\n"
            f"- 실행 신원: @{safe_actor}\n"
            f"- 설명: {safe_detail or '-'}\n\n"
            "이 댓글은 새 상태로 갱신됩니다."
        )

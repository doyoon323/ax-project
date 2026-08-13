from __future__ import annotations

import subprocess
from pathlib import Path

from issue_to_pr_agent.config import Settings
from issue_to_pr_agent.github_client import GitWorkspaceManager
from issue_to_pr_agent.models import IssueTask


def git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_worktree_publishes_only_explicit_agent_edits(tmp_path: Path) -> None:
    remote = tmp_path / "owner" / "repository.git"
    remote.parent.mkdir()
    git(tmp_path, "init", "--bare", "-q", str(remote))

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test")
    (seed / "sample.py").write_text("value = 1\n", encoding="utf-8")
    git(seed, "add", "sample.py")
    git(seed, "commit", "-qm", "baseline")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "-u", "origin", "main")

    target = tmp_path / "target"
    git(tmp_path, "clone", "-q", "-b", "main", str(remote), str(target))
    git(target, "config", "user.email", "test@example.com")
    git(target, "config", "user.name", "Test")
    settings = Settings(
        gemini_api_key="test-key",
        github_token="test-token",
        github_webhook_secret="test-secret",
        github_repository="owner/repository",
        workspace_path=target,
        worktree_root=tmp_path / "worktrees",
        state_db_path=tmp_path / "jobs.sqlite3",
        fetch_before_run=False,
    )
    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=3,
        title="Change value",
        body="Use value two.",
        author="octocat",
        author_association="OWNER",
    )
    manager = GitWorkspaceManager(settings)
    session = manager.prepare(issue)
    try:
        (session.path / "sample.py").write_text("value = 2\n", encoding="utf-8")
        (session.path / "generated.log").write_text("not for git\n", encoding="utf-8")

        changed = manager.commit_and_push(session, issue.number, ["sample.py"])

        assert changed == ["sample.py"]
        branch_tree = git(remote, "ls-tree", "-r", "--name-only", session.branch)
        assert branch_tree == "sample.py"
    finally:
        manager.cleanup(session)

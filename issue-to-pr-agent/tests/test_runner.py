from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from issue_to_pr_agent.runner import process_request
from issue_to_pr_agent.tools import WorkspaceTools


def test_runner_executes_only_verification_commands(tmp_path: Path) -> None:
    worktrees = tmp_path / "worktrees"
    workspace = worktrees / "issue-1"
    queue = tmp_path / "queue"
    request_dir = queue / "requests"
    response_dir = queue / "responses"
    workspace.mkdir(parents=True)
    request_dir.mkdir(parents=True)
    response_dir.mkdir(parents=True)
    (workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")

    def serve_once() -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            requests = list(request_dir.glob("*.json"))
            if requests:
                process_request(requests[0], response_dir, worktrees.resolve())
                return
            time.sleep(0.01)
        raise AssertionError("runner request was not created")

    thread = threading.Thread(target=serve_once)
    thread.start()
    result = WorkspaceTools(
        workspace,
        verification_backend="runner",
        verification_runner_queue_path=queue,
        runner_worktree_root=worktrees,
        verification_runner_poll_seconds=0.01,
        verification_timeout_seconds=10,
    ).run(["python", "-m", "compileall", "sample.py"], "verify")
    thread.join(timeout=2)

    assert result.succeeded
    assert result.is_verification


def test_runner_rejects_non_verification_request(tmp_path: Path) -> None:
    worktrees = tmp_path / "worktrees"
    workspace = worktrees / "issue-1"
    request_dir = tmp_path / "requests"
    response_dir = tmp_path / "responses"
    workspace.mkdir(parents=True)
    request_dir.mkdir()
    response_dir.mkdir()
    request_id = uuid.uuid4().hex
    request_path = request_dir / f"{request_id}.json"
    request_path.write_text(
        json.dumps(
            {
                "id": request_id,
                "workspace": "issue-1",
                "argv": ["ls"],
                "timeout_seconds": 10,
            }
        ),
        encoding="utf-8",
    )

    process_request(request_path, response_dir, worktrees.resolve())
    response = json.loads((response_dir / f"{request_id}.json").read_text(encoding="utf-8"))

    assert response["return_code"] == 126
    assert "Runner rejected request" in response["output"]

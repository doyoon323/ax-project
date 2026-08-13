from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import multiprocessing
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from issue_to_pr_agent.config import Settings
from issue_to_pr_agent.github_client import GitHubClient, GitHubPublishError
from issue_to_pr_agent.jobs import JobStore
from issue_to_pr_agent.main import create_app, extract_issue_task, verify_webhook_signature
from issue_to_pr_agent.models import AgentRunResult, CommandResult, IssueTask
from issue_to_pr_agent.worker import JobWorker


def mark_running_until_killed(db_path: str, issue: IssueTask, ready: Any) -> None:
    store = JobStore(Path(db_path))
    store.initialize()
    store.enqueue(issue)
    store.mark_running(issue.delivery_id)
    ready.set()
    time.sleep(30)


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
    )


def test_ollama_configuration_does_not_require_gemini_key(tmp_path: Path) -> None:
    settings = Settings(
        gemini_api_key="",
        github_token="test-token",
        github_webhook_secret="test-secret",
        github_repository="owner/repository",
        workspace_path=tmp_path,
        llm_model="ollama/qwen2.5-coder",
        llm_api_base="http://localhost:11434",
    )

    assert settings.gemini_api_key.get_secret_value() == ""


def test_poll_configuration_does_not_require_webhook_secret(tmp_path: Path) -> None:
    settings = Settings(
        gemini_api_key="test-key",
        github_repository="owner/repository",
        workspace_path=tmp_path,
        issue_source="poll",
    )

    assert settings.github_webhook_secret.get_secret_value() == ""


def test_publish_requires_expected_bot_identity(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="GITHUB_EXPECTED_LOGIN"):
        Settings(
            gemini_api_key="test-key",
            github_token="test-token",
            github_webhook_secret="test-secret",
            github_repository="owner/repository",
            workspace_path=tmp_path,
            publish_enabled=True,
        )


def test_github_token_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    class FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}
        content = b"{}"

        @staticmethod
        def json() -> dict[str, str]:
            return {"login": "unexpected-user"}

    class FakeSession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        @staticmethod
        def request(*_: object, **__: object) -> FakeResponse:
            return FakeResponse()

    settings = make_settings(tmp_path).model_copy(
        update={"github_expected_login": "issue-agent-bot", "publish_enabled": True}
    )
    client = GitHubClient(settings, session=FakeSession())  # type: ignore[arg-type]

    with pytest.raises(GitHubPublishError, match="identity mismatch"):
        client.validate_identity()


def test_github_verification_check_is_created_idempotently(tmp_path: Path) -> None:
    class FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}
        content = b"{}"

        def __init__(self, payload: dict, status_code: int = 200) -> None:
            self.payload = payload
            self.status_code = status_code

        def json(self) -> dict:
            return self.payload

    class FakeSession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.calls: list[tuple[str, str, dict]] = []

        def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            self.calls.append((method, url, kwargs))
            if method == "GET":
                return FakeResponse({"check_runs": []})
            return FakeResponse({}, status_code=201)

    session = FakeSession()
    client = GitHubClient(make_settings(tmp_path), session=session)  # type: ignore[arg-type]
    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=42,
        title="Fix a bug",
        body="Expected behavior",
        author="octocat",
        author_association="OWNER",
    )
    result = AgentRunResult(
        success=True,
        summary="fixed",
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
    )

    client.upsert_verification_check(issue, "a" * 40, result)

    assert [call[0] for call in session.calls] == ["GET", "POST"]
    assert session.calls[1][2]["json"]["conclusion"] == "success"
    assert session.calls[1][2]["json"]["external_id"] == issue.delivery_id


def issue_payload() -> dict:
    return {
        "action": "opened",
        "repository": {"full_name": "owner/repository"},
        "issue": {
            "number": 42,
            "title": "Fix a bug",
            "body": "Expected behavior",
            "author_association": "OWNER",
            "user": {"login": "octocat"},
            "labels": [{"name": "ai-fix"}],
        },
    }


def test_webhook_signature() -> None:
    body = b'{"action":"opened"}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(body, signature, "secret")
    assert not verify_webhook_signature(body, "sha256=wrong", "secret")


def test_event_filter_and_delivery_idempotency(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    issue = extract_issue_task(
        issue_payload(),
        event="issues",
        delivery_id="abcdef12-3456",
        settings=settings,
    )
    assert issue is not None

    store = JobStore(settings.state_db_path)
    store.initialize()
    assert store.enqueue(issue)
    assert not store.enqueue(issue)
    assert store.status(issue.delivery_id) == "queued"
    store.mark_completed(issue.delivery_id, {"status": "no-change"})
    assert store.status(issue.delivery_id) == "completed"


def test_job_store_accumulates_usage_across_attempts(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=42,
        title="Fix a bug",
        body="Expected behavior",
        author="octocat",
        author_association="OWNER",
    )
    store.enqueue(issue)
    store.record_usage(
        issue.delivery_id,
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        estimated_cost_usd=0.001,
    )
    store.record_usage(
        issue.delivery_id,
        prompt_tokens=200,
        completion_tokens=75,
        total_tokens=275,
        estimated_cost_usd=0.002,
    )

    usage = store.usage(issue.delivery_id)

    assert usage.prompt_tokens == 300
    assert usage.completion_tokens == 125
    assert usage.total_tokens == 425
    assert usage.estimated_cost_usd == pytest.approx(0.003)


def test_worker_retries_and_persists_attempt_count(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    attempts_path = tmp_path / "attempts.txt"

    class TransientFailure(RuntimeError):
        status_code = 503

    def flaky_processor(_: IssueTask) -> dict:
        attempts = int(attempts_path.read_text() or "0") if attempts_path.exists() else 0
        attempts_path.write_text(str(attempts + 1), encoding="utf-8")
        if attempts == 0:
            raise TransientFailure("temporary failure")
        return {"status": "processed"}

    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=42,
        title="Fix a bug",
        body="Expected behavior",
        author="octocat",
        author_association="OWNER",
    )
    worker = JobWorker(store, flaky_processor, max_attempts=2, retry_delay_seconds=0)

    async def exercise_worker() -> None:
        await worker.start()
        await worker.submit(issue)
        await worker.queue.join()
        await worker.stop()

    asyncio.run(exercise_worker())

    assert attempts_path.read_text(encoding="utf-8") == "2"
    assert store.status(issue.delivery_id) == "completed"
    assert store.attempt_count(issue.delivery_id) == 2


def test_worker_does_not_retry_deterministic_failure(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    attempts_path = tmp_path / "attempts.txt"

    def invalid_job(_: IssueTask) -> dict:
        attempts_path.write_text("attempted", encoding="utf-8")
        raise ValueError("invalid patch")

    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=42,
        title="Fix a bug",
        body="Expected behavior",
        author="octocat",
        author_association="OWNER",
    )
    worker = JobWorker(store, invalid_job, max_attempts=3, retry_delay_seconds=0)

    async def exercise_worker() -> None:
        await worker.start()
        await worker.submit(issue)
        await worker.queue.join()
        await worker.stop()

    asyncio.run(exercise_worker())

    assert attempts_path.read_text(encoding="utf-8") == "attempted"
    assert store.status(issue.delivery_id) == "failed"


def test_worker_hard_timeout_kills_isolated_process(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    pid_path = tmp_path / "worker.pid"

    def hanging_job(_: IssueTask) -> dict:
        with pid_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{os.getpid()}\n")
        time.sleep(30)
        return {"status": "unexpected"}

    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=42,
        title="Hang",
        body="Never completes",
        author="octocat",
        author_association="OWNER",
    )
    worker = JobWorker(
        store,
        hanging_job,
        max_attempts=2,
        retry_delay_seconds=0,
        process_timeout_seconds=0.2,
    )

    async def exercise_worker() -> None:
        await worker.start()
        await worker.submit(issue)
        await worker.queue.join()
        await worker.stop()

    asyncio.run(exercise_worker())

    assert store.status(issue.delivery_id) == "failed"
    assert store.attempt_count(issue.delivery_id) == 2
    assert "JobProcessTimeoutError" in store.error(issue.delivery_id)
    process_ids = [int(item) for item in pid_path.read_text(encoding="utf-8").splitlines()]
    assert len(process_ids) == 2
    for process_id in process_ids:
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, 0)


def test_worker_shutdown_requeues_interrupted_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    started_path = tmp_path / "started"

    def hanging_job(_: IssueTask) -> dict:
        started_path.touch()
        time.sleep(30)
        return {"status": "unexpected"}

    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=42,
        title="Hang",
        body="Never completes",
        author="octocat",
        author_association="OWNER",
    )
    worker = JobWorker(
        store,
        hanging_job,
        process_timeout_seconds=30,
        shutdown_timeout_seconds=0.2,
    )

    async def exercise_worker() -> None:
        await worker.start()
        await worker.submit(issue)
        for _ in range(100):
            if started_path.exists():
                break
            await asyncio.sleep(0.01)
        assert started_path.exists()
        await worker.stop()

    asyncio.run(exercise_worker())

    assert store.status(issue.delivery_id) == "queued"


def test_running_job_is_recovered_within_retry_budget(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=42,
        title="Fix a bug",
        body="Expected behavior",
        author="octocat",
        author_association="OWNER",
    )
    store.enqueue(issue)
    assert store.mark_running(issue.delivery_id) == 1

    recovery = store.recover(max_attempts=2)
    assert recovery.queued == [issue.delivery_id]
    assert recovery.exhausted == []
    assert store.status(issue.delivery_id) == "queued"


def test_abrupt_process_kill_recovers_running_job(tmp_path: Path) -> None:
    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=42,
        title="Fix a bug",
        body="Expected behavior",
        author="octocat",
        author_association="OWNER",
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(
        target=mark_running_until_killed,
        args=(str(tmp_path / "jobs.sqlite3"), issue, ready),
    )
    process.start()
    try:
        assert ready.wait(timeout=10)
        process.kill()
        process.join(timeout=10)
        assert process.exitcode is not None and process.exitcode != 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)

    store = JobStore(tmp_path / "jobs.sqlite3")
    recovery = store.recover(max_attempts=2)

    assert recovery.queued == [issue.delivery_id]
    assert store.status(issue.delivery_id) == "queued"


def test_running_job_is_failed_when_restart_budget_is_exhausted(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    issue = IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=42,
        title="Fix a bug",
        body="Expected behavior",
        author="octocat",
        author_association="OWNER",
    )
    store.enqueue(issue)
    assert store.mark_running(issue.delivery_id) == 1

    recovery = store.recover(max_attempts=1)

    assert recovery.queued == []
    assert recovery.exhausted == [issue.delivery_id]
    assert store.status(issue.delivery_id) == "failed"


def test_github_status_comment_is_stable_and_sanitized() -> None:
    body = GitHubClient._status_comment_body(
        status="retrying",
        attempt=1,
        max_attempts=2,
        detail="temporary\nfailure",
        actor="issue-agent-bot",
    )

    assert "<!-- issue-to-pr-agent-status -->" in body
    assert "재시도 대기" in body
    assert "temporary failure" in body
    assert "@issue-agent-bot" in body


def test_untrusted_author_is_ignored(tmp_path: Path) -> None:
    payload = issue_payload()
    payload["issue"]["author_association"] = "NONE"

    assert (
        extract_issue_task(
            payload,
            event="issues",
            delivery_id="abcdef12-3456",
            settings=make_settings(tmp_path),
        )
        is None
    )


def test_webhook_endpoint_queues_once(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    raw_body = json.dumps(issue_payload()).encode()
    signature = "sha256=" + hmac.new(b"test-secret", raw_body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "abcdef12-3456",
        "Content-Type": "application/json",
    }
    app = create_app(settings, processor=lambda _: {"status": "processed"})

    async def exercise_endpoint() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.post("/webhook", content=raw_body, headers=headers)
                duplicate = await client.post("/webhook", content=raw_body, headers=headers)
        return first, duplicate

    first, duplicate = asyncio.run(exercise_endpoint())

    assert first.status_code == 200
    assert first.json()["status"] == "queued"
    assert duplicate.json()["status"] == "duplicate"


def test_poll_payload_becomes_deterministic_issue_task(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = GitHubClient(settings)
    payload = issue_payload()["issue"] | {"updated_at": "2026-08-07T00:00:00Z"}

    first = client._to_issue_task(payload)
    second = client._to_issue_task(payload)

    assert first is not None
    assert first.delivery_id.startswith("poll-")
    assert second == first
    updated = client._to_issue_task(payload | {"updated_at": "2026-08-08T00:00:00Z"})
    assert updated == first
    revised = client._to_issue_task(payload | {"body": "Clarified expected behavior"})
    assert revised is not None
    assert revised.delivery_id != first.delivery_id


def test_poll_ignores_pull_requests_and_untrusted_authors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = GitHubClient(settings)
    payload = issue_payload()["issue"] | {"updated_at": "2026-08-07T00:00:00Z"}

    assert client._to_issue_task(payload | {"pull_request": {}}) is None
    assert client._to_issue_task(payload | {"author_association": "NONE"}) is None

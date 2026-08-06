from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import httpx

from issue_to_pr_agent.config import Settings
from issue_to_pr_agent.github_client import GitHubClient
from issue_to_pr_agent.jobs import JobStore
from issue_to_pr_agent.main import create_app, extract_issue_task, verify_webhook_signature


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
    assert store.has_completed_issue(issue.repository, issue.number)


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


def test_poll_ignores_pull_requests_and_untrusted_authors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = GitHubClient(settings)
    payload = issue_payload()["issue"] | {"updated_at": "2026-08-07T00:00:00Z"}

    assert client._to_issue_task(payload | {"pull_request": {}}) is None
    assert client._to_issue_task(payload | {"author_association": "NONE"}) is None

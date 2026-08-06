from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .config import Settings, get_settings
from .github_client import GitHubClient
from .jobs import JobStore
from .models import IssueTask
from .poller import IssuePoller
from .service import IssueToPRService
from .worker import JobWorker

_DELIVERY_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{6,100}$")


def verify_webhook_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def extract_issue_task(
    payload: dict[str, Any],
    *,
    event: str,
    delivery_id: str,
    settings: Settings,
) -> IssueTask | None:
    if event != "issues":
        return None
    action = payload.get("action")
    if action not in {"opened", "labeled"}:
        return None
    if not _DELIVERY_ID_PATTERN.fullmatch(delivery_id):
        raise ValueError("invalid X-GitHub-Delivery header")

    repository = payload.get("repository") or {}
    if repository.get("full_name") != settings.github_repository:
        raise ValueError("webhook repository does not match GITHUB_REPOSITORY")

    issue = payload.get("issue") or {}
    labels = {
        str(item.get("name"))
        for item in issue.get("labels", [])
        if isinstance(item, dict) and item.get("name")
    }
    required_label = settings.required_issue_label
    if required_label:
        if required_label not in labels:
            return None
        if action == "labeled" and (payload.get("label") or {}).get("name") != required_label:
            return None

    association = str(issue.get("author_association") or "").upper()
    if association not in settings.allowed_associations:
        return None

    user = issue.get("user") or {}
    number = issue.get("number")
    title = issue.get("title")
    author = user.get("login")
    if not isinstance(number, int) or not isinstance(title, str) or not isinstance(author, str):
        raise ValueError("webhook issue payload is missing required fields")
    body = issue.get("body")
    if body is not None and not isinstance(body, str):
        raise ValueError("issue body must be text or null")

    return IssueTask(
        delivery_id=delivery_id,
        repository=settings.github_repository,
        number=number,
        title=title,
        body=body or "",
        author=author,
        author_association=association,
    )


def create_app(
    settings: Settings | None = None,
    processor: Callable[[IssueTask], dict[str, Any]] | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    store = JobStore(active_settings.state_db_path)
    service: IssueToPRService | None = None
    if processor is None:
        service = IssueToPRService(active_settings)
        active_processor = service.process
    else:
        active_processor = processor
    worker = JobWorker(store, active_processor)
    poller = (
        IssuePoller(
            service.github if service is not None else GitHubClient(active_settings),
            worker,
            active_settings.poll_interval_seconds,
        )
        if active_settings.issue_source == "poll"
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await worker.start()
        if poller is not None:
            await poller.start()
        try:
            yield
        finally:
            if poller is not None:
                await poller.stop()
            await worker.stop()

    app = FastAPI(title="Issue-to-PR Agent", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "issue_source": active_settings.issue_source,
            "publish_enabled": active_settings.publish_enabled,
        }

    @app.post("/webhook")
    async def webhook(request: Request) -> dict[str, Any]:
        raw_body = await request.body()
        if len(raw_body) > 1_000_000:
            raise HTTPException(status_code=413, detail="webhook payload is too large")
        signature = request.headers.get("X-Hub-Signature-256")
        secret = active_settings.github_webhook_secret.get_secret_value()
        if not verify_webhook_signature(raw_body, signature, secret):
            raise HTTPException(status_code=401, detail="invalid webhook signature")

        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return {"status": "pong"}
        delivery_id = request.headers.get("X-GitHub-Delivery", "")
        try:
            payload = json.loads(raw_body)
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            issue = extract_issue_task(
                payload,
                event=event,
                delivery_id=delivery_id,
                settings=active_settings,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if issue is None:
            return {"status": "ignored"}
        inserted = await worker.submit(issue)
        if not inserted:
            return {
                "status": "duplicate",
                "issue_number": issue.number,
                "delivery_id": delivery_id,
            }
        return {
            "status": "queued",
            "issue_number": issue.number,
            "delivery_id": delivery_id,
        }

    return app

from __future__ import annotations

import asyncio
import logging

from .github_client import GitHubClient
from .worker import JobWorker

logger = logging.getLogger(__name__)


class IssuePoller:
    """Poll trusted, labeled GitHub Issues and submit unseen revisions."""

    def __init__(
        self,
        github: GitHubClient,
        worker: JobWorker,
        interval_seconds: float,
    ) -> None:
        self.github = github
        self.worker = worker
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="issue-poller")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def scan_once(self) -> int:
        issues = await asyncio.to_thread(self.github.list_candidate_issues)
        inserted = 0
        for issue in issues:
            if self.worker.store.has_completed_issue(issue.repository, issue.number):
                continue
            if await self.worker.submit(issue):
                inserted += 1
                logger.info("Detected GitHub Issue #%s and queued it", issue.number)
        return inserted

    async def _run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("GitHub Issue polling failed; retrying on next interval")
            await asyncio.sleep(self.interval_seconds)

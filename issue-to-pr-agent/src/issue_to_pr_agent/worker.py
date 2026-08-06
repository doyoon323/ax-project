from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .jobs import JobStore
from .models import IssueTask

logger = logging.getLogger(__name__)


class JobWorker:
    """Single-consumer worker so LLM calls and Git worktrees never race locally."""

    def __init__(
        self,
        store: JobStore,
        processor: Callable[[IssueTask], dict[str, Any]],
    ) -> None:
        self.store = store
        self.processor = processor
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.store.initialize()
        for delivery_id in self.store.recover():
            await self.queue.put(delivery_id)
        self._task = asyncio.create_task(self._run(), name="issue-to-pr-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        # Let the active thread and all accepted jobs finish before cancelling the idle consumer.
        await self.queue.join()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def submit(self, issue: IssueTask) -> bool:
        inserted = self.store.enqueue(issue)
        if inserted:
            await self.queue.put(issue.delivery_id)
        return inserted

    async def _run(self) -> None:
        while True:
            delivery_id = await self.queue.get()
            try:
                issue = self.store.get_issue(delivery_id)
                self.store.mark_running(delivery_id)
                result = await asyncio.to_thread(self.processor, issue)
                self.store.mark_completed(delivery_id, result)
                logger.info("Issue #%s completed with status=%s", issue.number, result["status"])
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ) as exc:  # The job boundary must persist failures without killing the worker.
                self.store.mark_failed(delivery_id, f"{type(exc).__name__}: {exc}")
                logger.exception("Issue-to-PR job failed for delivery %s", delivery_id)
            finally:
                self.queue.task_done()

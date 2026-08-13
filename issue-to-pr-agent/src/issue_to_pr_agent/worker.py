from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .jobs import JobStore
from .models import IssueTask

logger = logging.getLogger(__name__)

StatusCallback = Callable[[IssueTask, str, int, int, str], None]


class JobWorker:
    """Single-consumer worker so LLM calls and Git worktrees never race locally."""

    def __init__(
        self,
        store: JobStore,
        processor: Callable[[IssueTask], dict[str, Any]],
        *,
        max_attempts: int = 2,
        retry_delay_seconds: float = 10.0,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.store = store
        self.processor = processor
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.status_callback = status_callback
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.store.initialize()
        for delivery_id in self.store.recover(self.max_attempts):
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
                attempt = self.store.mark_running(delivery_id)
                await self._notify(issue, "running", attempt, "작업을 시작했습니다.")
                result = await asyncio.to_thread(self.processor, issue)
                self.store.mark_completed(delivery_id, result)
                await self._notify(
                    issue,
                    "completed",
                    attempt,
                    f"작업 결과: {result.get('status', 'completed')}",
                )
                logger.info("Issue #%s completed with status=%s", issue.number, result["status"])
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ) as exc:  # The job boundary must persist failures without killing the worker.
                error = f"{type(exc).__name__}: {exc}"
                attempt = self.store.attempt_count(delivery_id)
                issue = self.store.get_issue(delivery_id)
                if attempt < self.max_attempts and self._is_retryable(exc):
                    self.store.mark_retry_queued(delivery_id, error)
                    await self._notify(
                        issue,
                        "retrying",
                        attempt,
                        "일시 실패로 자동 재시도합니다.",
                    )
                    logger.exception(
                        "Issue-to-PR job attempt %s/%s failed for delivery %s; retrying",
                        attempt,
                        self.max_attempts,
                        delivery_id,
                    )
                    await asyncio.sleep(self.retry_delay_seconds * attempt)
                    await self.queue.put(delivery_id)
                else:
                    self.store.mark_failed(delivery_id, error)
                    await self._notify(
                        issue,
                        "failed",
                        attempt,
                        (
                            "자동 재시도 한도를 소진했습니다. 로컬 로그를 확인하세요."
                            if attempt >= self.max_attempts
                            else (
                                "자동 재시도 대상이 아닌 오류입니다. 원인을 보완한 뒤 "
                                "Issue 제목 또는 본문을 수정해 새 revision으로 접수하세요."
                            )
                        ),
                    )
                    logger.exception("Issue-to-PR job failed for delivery %s", delivery_id)
            finally:
                self.queue.task_done()

    async def _notify(self, issue: IssueTask, status: str, attempt: int, detail: str) -> None:
        if self.status_callback is None:
            return
        try:
            await asyncio.to_thread(
                self.status_callback,
                issue,
                status,
                attempt,
                self.max_attempts,
                detail,
            )
        except Exception:
            logger.exception("GitHub-visible status update failed for Issue #%s", issue.number)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if bool(getattr(exc, "retryable", False)):
            return True
        status_code = getattr(exc, "status_code", None)
        return status_code == 429 or status_code in {408, 500, 502, 503, 504}

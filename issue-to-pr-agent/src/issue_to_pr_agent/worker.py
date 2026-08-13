from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import queue
import signal
from collections.abc import Callable
from typing import Any

from .jobs import JobStore
from .models import IssueTask

logger = logging.getLogger(__name__)

StatusCallback = Callable[[IssueTask, str, int, int, str], None]


class JobProcessError(RuntimeError):
    """Failure returned by the isolated job process."""

    def __init__(
        self,
        remote_type: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"{remote_type}: {message}")
        self.remote_type = remote_type
        self.status_code = status_code
        self.retryable = retryable


class JobProcessTimeoutError(JobProcessError):
    """Raised after the parent kills a job process that exceeded its hard deadline."""


def _run_processor_process(
    processor: Callable[[IssueTask], dict[str, Any]],
    issue: IssueTask,
    result_queue: Any,
) -> None:
    os.setsid()
    try:
        result_queue.put({"ok": True, "result": processor(issue)})
    except BaseException as exc:
        result_queue.put(
            {
                "ok": False,
                "type": type(exc).__name__,
                "message": str(exc)[:1_000],
                "status_code": getattr(exc, "status_code", None),
                "retryable": bool(getattr(exc, "retryable", False)),
            }
        )


class JobWorker:
    """Single-consumer worker so LLM calls and Git worktrees never race locally."""

    def __init__(
        self,
        store: JobStore,
        processor: Callable[[IssueTask], dict[str, Any]],
        *,
        max_attempts: int = 3,
        retry_delay_seconds: float = 10.0,
        process_timeout_seconds: float = 600.0,
        shutdown_timeout_seconds: float = 20.0,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.store = store
        self.processor = processor
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.process_timeout_seconds = process_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.status_callback = status_callback
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.store.initialize()
        recovery = self.store.recover(self.max_attempts)
        for delivery_id in recovery.exhausted:
            issue = self.store.get_issue(delivery_id)
            await self._notify(
                issue,
                "failed",
                self.store.attempt_count(delivery_id),
                "서비스 중단 후 재시도 한도가 소진되어 자동 게시를 중단했습니다.",
            )
        for delivery_id in recovery.queued:
            await self.queue.put(delivery_id)
        self._task = asyncio.create_task(self._run(), name="issue-to-pr-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self.queue.join(), timeout=self.shutdown_timeout_seconds)
        except TimeoutError:
            logger.warning("Worker shutdown deadline reached; terminating the active job process")
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
                result = await self._run_in_process(issue)
                self.store.mark_completed(delivery_id, result)
                await self._notify(
                    issue,
                    "completed",
                    attempt,
                    f"작업 결과: {result.get('status', 'completed')}",
                )
                logger.info("Issue #%s completed with status=%s", issue.number, result["status"])
            except asyncio.CancelledError:
                if "delivery_id" in locals():
                    self.store.mark_retry_queued(
                        delivery_id,
                        "service shutdown interrupted the isolated job process",
                    )
                raise
            except (
                Exception
            ) as exc:  # The job boundary must persist failures without killing the worker.
                error = f"{type(exc).__name__}: {exc}"
                attempt = self.store.attempt_count(delivery_id)
                issue = self.store.get_issue(delivery_id)
                retryable = self._is_retryable(exc)
                if attempt < self.max_attempts and retryable:
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
                        self._failure_detail(exc, attempt),
                        max_attempts=self.max_attempts if retryable else attempt,
                    )
                    logger.exception("Issue-to-PR job failed for delivery %s", delivery_id)
            finally:
                self.queue.task_done()

    async def _notify(
        self,
        issue: IssueTask,
        status: str,
        attempt: int,
        detail: str,
        *,
        max_attempts: int | None = None,
    ) -> None:
        if self.status_callback is None:
            return
        try:
            await asyncio.to_thread(
                self.status_callback,
                issue,
                status,
                attempt,
                self.max_attempts if max_attempts is None else max_attempts,
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

    async def _run_in_process(self, issue: IssueTask) -> dict[str, Any]:
        context = multiprocessing.get_context("fork")
        result_queue = context.Queue(maxsize=1)
        process = context.Process(
            target=_run_processor_process,
            args=(self.processor, issue, result_queue),
            name=f"issue-agent-{issue.number}",
        )
        process.start()
        deadline = asyncio.get_running_loop().time() + self.process_timeout_seconds
        try:
            while process.is_alive():
                if asyncio.get_running_loop().time() >= deadline:
                    self._kill_process_group(process)
                    raise JobProcessTimeoutError(
                        "JobProcessTimeoutError",
                        f"job process exceeded {self.process_timeout_seconds}s hard deadline",
                        retryable=True,
                    )
                await asyncio.sleep(0.05)
            process.join(timeout=1)
            try:
                payload = result_queue.get(timeout=1)
            except queue.Empty as exc:
                raise JobProcessError(
                    "JobProcessExitError",
                    f"job process exited with code {process.exitcode} without a result",
                ) from exc
            if payload.get("ok"):
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise JobProcessError(
                        "InvalidJobResult", "processor result must be a dictionary"
                    )
                return result
            raise JobProcessError(
                str(payload.get("type") or "RemoteJobError"),
                str(payload.get("message") or "isolated job process failed"),
                status_code=payload.get("status_code"),
                retryable=bool(payload.get("retryable", False)),
            )
        except asyncio.CancelledError:
            self._kill_process_group(process)
            raise
        finally:
            if process.is_alive():
                self._kill_process_group(process)
            process.join(timeout=1)
            result_queue.close()

    @staticmethod
    def _kill_process_group(process: multiprocessing.Process) -> None:
        if process.pid is None or not process.is_alive():
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)

    def _failure_detail(self, exc: Exception, attempt: int) -> str:
        name = str(getattr(exc, "remote_type", type(exc).__name__))
        message = str(exc).casefold()
        if "edited tests also pass against the base commit" in message:
            reason = (
                "추가한 테스트가 수정 전 코드에서도 통과해 Issue의 요구사항을 "
                "증명하지 못했습니다. 기대 동작과 인수 조건을 구체적으로 작성해 주세요."
            )
        elif "baseline test failed because of an import" in message:
            reason = (
                "수정 전 코드에서 테스트를 정상적으로 불러오지 못해 안전한 "
                "fail-to-pass 증거를 만들지 못했습니다."
            )
        elif "patch produced no effective file changes" in message:
            reason = "실제 코드 변경을 만들지 못해 자동 게시를 중단했습니다."
        elif "verification failed" in message:
            reason = "수정 코드가 필수 테스트를 통과하지 못해 자동 게시를 중단했습니다."
        elif "Timeout" in name:
            reason = "실행 시간 한도를 초과해 중단했습니다."
        elif "Budget" in name:
            reason = "토큰 또는 비용 한도를 초과해 중단했습니다."
        elif "complexity limit exceeded" in message or "Complexity" in name:
            reason = "자동 수정 범위를 초과해 사람 검토로 전환했습니다."
        elif "AgentExecution" in name:
            reason = "안전한 자동 검증 기준을 충족하지 못해 게시를 중단했습니다."
        else:
            reason = "안전하게 자동 처리할 수 없는 오류로 중단했습니다."
        if self._is_retryable(exc):
            retry = (
                " 자동 재시도 한도를 소진했습니다."
                if attempt >= self.max_attempts
                else " 일시 오류이므로 자동 재시도할 수 있습니다."
            )
        else:
            retry = (
                " 이 오류는 자동 재시도하지 않습니다. Issue를 수정하면 새 revision으로 "
                "다시 접수됩니다."
            )
        return f"{reason}{retry} 오류 분류: {name}."

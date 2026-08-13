from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import IssueTask


@dataclass(frozen=True)
class RecoveryResult:
    queued: list[str]
    exhausted: list[str]


class JobStore:
    """Small durable queue state used for webhook delivery idempotency."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=False)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    delivery_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
                    result_json TEXT,
                    error TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "attempt_count" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
                )

    def enqueue(self, issue: IssueTask) -> bool:
        payload = json.dumps(asdict(issue), ensure_ascii=False)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO jobs (delivery_id, payload_json, status)
                VALUES (?, ?, 'queued')
                """,
                (issue.delivery_id, payload),
            )
            return cursor.rowcount == 1

    def recover(self, max_attempts: int) -> RecoveryResult:
        with self._connect() as connection:
            exhausted_rows = connection.execute(
                """
                SELECT delivery_id FROM jobs
                WHERE status IN ('queued', 'running') AND attempt_count >= ?
                ORDER BY created_at
                """,
                (max_attempts,),
            ).fetchall()
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    error = COALESCE(error, 'retry budget exhausted during restart recovery'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE status IN ('queued', 'running') AND attempt_count >= ?
                """,
                (max_attempts,),
            )
            connection.execute(
                """
                UPDATE jobs SET status = 'queued', updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running' AND attempt_count < ?
                """,
                (max_attempts,),
            )
            rows = connection.execute(
                "SELECT delivery_id FROM jobs WHERE status = 'queued' ORDER BY created_at"
            ).fetchall()
        return RecoveryResult(
            queued=[str(row[0]) for row in rows],
            exhausted=[str(row[0]) for row in exhausted_rows],
        )

    def get_issue(self, delivery_id: str) -> IssueTask:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return IssueTask(**json.loads(row[0]))

    def mark_running(self, delivery_id: str) -> int:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'running', attempt_count = attempt_count + 1,
                    error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            )
            row = connection.execute(
                "SELECT attempt_count FROM jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return int(row[0])

    def mark_retry_queued(self, delivery_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'queued', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (error[:1_000], delivery_id),
            )

    def mark_completed(self, delivery_id: str, result: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'completed', result_json = ?, error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (json.dumps(result, ensure_ascii=False), delivery_id),
            )

    def mark_failed(self, delivery_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (error[:1_000], delivery_id),
            )

    def status(self, delivery_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def attempt_count(self, delivery_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return int(row[0])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

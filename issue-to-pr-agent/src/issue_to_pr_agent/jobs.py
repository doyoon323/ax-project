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


@dataclass(frozen=True)
class UsageLedger:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


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
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0,
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
            migrations = {
                "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
                "completion_tokens": "INTEGER NOT NULL DEFAULT 0",
                "total_tokens": "INTEGER NOT NULL DEFAULT 0",
                "estimated_cost_usd": "REAL NOT NULL DEFAULT 0",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

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

    def save_checkpoint(self, delivery_id: str, checkpoint: dict[str, Any]) -> None:
        """Persist resumable work before an external publication boundary."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET result_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (json.dumps(checkpoint, ensure_ascii=False), delivery_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(delivery_id)

    def checkpoint(self, delivery_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        if row[0] is None:
            return None
        value = json.loads(str(row[0]))
        if not isinstance(value, dict):
            raise ValueError("job checkpoint must be a JSON object")
        return value

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

    def error(self, delivery_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT error FROM jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return str(row[0] or "")

    def record_usage(
        self,
        delivery_id: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated_cost_usd: float,
    ) -> None:
        if min(prompt_tokens, completion_tokens, total_tokens) < 0 or estimated_cost_usd < 0:
            raise ValueError("usage increments cannot be negative")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET prompt_tokens = prompt_tokens + ?,
                    completion_tokens = completion_tokens + ?,
                    total_tokens = total_tokens + ?,
                    estimated_cost_usd = estimated_cost_usd + ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    estimated_cost_usd,
                    delivery_id,
                ),
            )
        if cursor.rowcount != 1:
            raise KeyError(delivery_id)

    def usage(self, delivery_id: str) -> UsageLedger:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd
                FROM jobs WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return UsageLedger(
            prompt_tokens=int(row[0]),
            completion_tokens=int(row[1]),
            total_tokens=int(row[2]),
            estimated_cost_usd=float(row[3]),
        )

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

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import IssueTask


class JobStore:
    """Small durable queue state used for webhook delivery idempotency."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=False)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    delivery_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
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

    def recover(self) -> list[str]:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'queued', updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                """
            )
            rows = connection.execute(
                "SELECT delivery_id FROM jobs WHERE status = 'queued' ORDER BY created_at"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def get_issue(self, delivery_id: str) -> IssueTask:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return IssueTask(**json.loads(row[0]))

    def mark_running(self, delivery_id: str) -> None:
        self._set_status(delivery_id, "running")

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

    def has_completed_issue(self, repository: str, issue_number: int) -> bool:
        """Return whether any prior revision of this Issue completed successfully."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM jobs WHERE status = 'completed'"
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("repository") == repository and payload.get("number") == issue_number:
                return True
        return False

    def _set_status(self, delivery_id: str, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (status, delivery_id),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

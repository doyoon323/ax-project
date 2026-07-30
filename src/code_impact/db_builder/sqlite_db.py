"""SQLite storage for one indexed Git commit."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from code_impact.db_builder.records import CallRecord, ChangedFunction, FunctionRecord


class AnalysisDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_schema(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS functions (
                    function_id TEXT PRIMARY KEY,
                    module_name TEXT NOT NULL,
                    function_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    is_test INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS calls (
                    caller_id TEXT NOT NULL,
                    callee_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    PRIMARY KEY (caller_id, callee_id, line)
                );

                CREATE TABLE IF NOT EXISTS changed_functions (
                    function_id TEXT PRIMARY KEY,
                    changed_line INTEGER NOT NULL
                );
                """
            )

    def replace_index(
        self,
        commit: str,
        functions: list[FunctionRecord],
        calls: list[CallRecord],
        changed_functions: list[ChangedFunction],
    ) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM metadata")
            connection.execute("DELETE FROM calls")
            connection.execute("DELETE FROM changed_functions")
            connection.execute("DELETE FROM functions")
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('commit', ?)",
                (commit,),
            )
            connection.executemany(
                """
                INSERT INTO functions(
                    function_id, module_name, function_name, file_path,
                    start_line, end_line, is_test
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.function_id,
                        item.module_name,
                        item.function_name,
                        item.file_path,
                        item.start_line,
                        item.end_line,
                        int(item.is_test),
                    )
                    for item in functions
                ],
            )
            connection.executemany(
                """
                INSERT INTO calls(caller_id, callee_id, file_path, line)
                VALUES (?, ?, ?, ?)
                """,
                [(item.caller_id, item.callee_id, item.file_path, item.line) for item in calls],
            )
            connection.executemany(
                """
                INSERT INTO changed_functions(function_id, changed_line)
                VALUES (?, ?)
                """,
                [(item.function.function_id, item.changed_line) for item in changed_functions],
            )

    @staticmethod
    def _function_from_row(row: sqlite3.Row) -> FunctionRecord:
        return FunctionRecord(
            function_id=row["function_id"],
            module_name=row["module_name"],
            function_name=row["function_name"],
            file_path=row["file_path"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            is_test=bool(row["is_test"]),
        )

    def list_changed_functions(self) -> list[ChangedFunction]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.*, c.changed_line
                FROM changed_functions c
                JOIN functions f ON f.function_id = c.function_id
                WHERE f.is_test = 0
                ORDER BY f.file_path, f.start_line
                """
            ).fetchall()
        return [
            ChangedFunction(
                function=self._function_from_row(row),
                changed_line=row["changed_line"],
            )
            for row in rows
        ]

    def callers_of(self, function_ids: list[str]) -> list[tuple[FunctionRecord, CallRecord]]:
        if not function_ids:
            return []
        placeholders = ",".join("?" for _ in function_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT f.*, c.callee_id, c.file_path AS call_file, c.line AS call_line
                FROM calls c
                JOIN functions f ON f.function_id = c.caller_id
                WHERE c.callee_id IN ({placeholders})
                ORDER BY f.file_path, c.line
                """,
                function_ids,
            ).fetchall()
        return [
            (
                self._function_from_row(row),
                CallRecord(
                    caller_id=row["function_id"],
                    callee_id=row["callee_id"],
                    file_path=row["call_file"],
                    line=row["call_line"],
                ),
            )
            for row in rows
        ]

    def test_callers_of(
        self,
        function_ids: list[str],
    ) -> list[tuple[FunctionRecord, CallRecord]]:
        return [
            (function, call) for function, call in self.callers_of(function_ids) if function.is_test
        ]

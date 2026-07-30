"""Read-only Git operations used by the analyzer."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from code_impact.db_builder.records import ChangedRange


class GitRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        if not (self.path / ".git").exists():
            raise ValueError(f"Not a Git repository: {self.path}")

    def _run(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout

    def resolve_ref(self, ref: str) -> str:
        return self._run("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def parent_of(self, commit: str) -> str:
        return self.resolve_ref(f"{commit}^")

    def list_python_files(self, commit: str) -> list[str]:
        output = self._run("ls-tree", "-r", "--name-only", commit)
        return sorted(path for path in output.splitlines() if path.endswith(".py"))

    def show_file(self, commit: str, file_path: str) -> str:
        return self._run("show", f"{commit}:{file_path}")

    def line_at(self, commit: str, file_path: str, line: int) -> str | None:
        lines = self.show_file(commit, file_path).splitlines()
        if line < 1 or line > len(lines):
            return None
        return lines[line - 1]

    def changed_python_ranges(self, commit: str) -> list[ChangedRange]:
        parent = self.parent_of(commit)
        diff = self._run(
            "diff",
            "--no-ext-diff",
            "--unified=0",
            parent,
            commit,
            "--",
            "*.py",
        )

        current_file: str | None = None
        ranges: list[ChangedRange] = []
        hunk_pattern = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")

        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                current_file = line.removeprefix("+++ b/")
                continue
            if not current_file:
                continue
            match = hunk_pattern.match(line)
            if not match:
                continue

            start = int(match.group("start"))
            count = int(match.group("count") or "1")
            if count == 0:
                continue
            ranges.append(
                ChangedRange(
                    file_path=current_file,
                    start_line=start,
                    end_line=start + count - 1,
                )
            )

        return ranges

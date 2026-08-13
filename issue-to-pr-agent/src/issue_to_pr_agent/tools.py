from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path

from .models import CommandResult, FileEdit, Phase


class ToolPolicyError(ValueError):
    """Raised when an LLM-requested action violates the local policy."""


class EditError(ValueError):
    """Raised when a structured edit cannot be applied exactly once."""


class ComplexityLimitError(RuntimeError):
    """Raised when an issue exceeds the bounded automatic-change policy."""


_READ_COMMANDS = {"head", "ls", "rg", "sed", "tail"}
_GIT_READ_SUBCOMMANDS = {"diff", "grep", "log", "ls-files", "show", "status"}
_VERIFY_COMMANDS = {"pytest", "ruff"}
_DENIED_EDIT_NAMES = {
    ".env",
    "credentials",
    "id_ed25519",
    "id_rsa",
}
_DENIED_EDIT_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
_DENIED_AUTOMATION_PATHS = {
    Path(".github/actions"),
    Path(".github/workflows"),
}


def truncate_output(text: str, max_chars: int = 1_000, max_lines: int = 50) -> str:
    """Keep useful head/tail context while respecting both output limits."""

    marker = "\n[...Output Truncated...]\n"
    lines = text.splitlines()
    if len(lines) > max_lines:
        head_count = max(1, max_lines * 7 // 10)
        tail_count = max(1, max_lines - head_count - 1)
        text = "\n".join(lines[:head_count]) + marker + "\n".join(lines[-tail_count:])

    if len(text) > max_chars:
        remaining = max_chars - len(marker)
        head_chars = max(1, remaining * 7 // 10)
        tail_chars = max(1, remaining - head_chars)
        text = text[:head_chars] + marker + text[-tail_chars:]
    return text


class WorkspaceTools:
    """Policy-enforced commands and exact edits for one isolated worktree."""

    def __init__(
        self,
        root: Path,
        *,
        timeout_seconds: int = 30,
        verification_timeout_seconds: int = 120,
        max_output_chars: int = 1_000,
        max_output_lines: int = 50,
        verification_backend: str = "host",
        verification_container_image: str = "python:3.13-slim",
        verification_runner_queue_path: Path = Path("/runner-queue"),
        runner_worktree_root: Path | None = None,
        verification_runner_poll_seconds: float = 0.1,
    ) -> None:
        self.root = root.resolve(strict=True)
        self.timeout_seconds = timeout_seconds
        self.verification_timeout_seconds = verification_timeout_seconds
        self.max_output_chars = max_output_chars
        self.max_output_lines = max_output_lines
        self.verification_backend = verification_backend
        self.verification_container_image = verification_container_image
        self.verification_runner_queue_path = verification_runner_queue_path
        self.runner_worktree_root = (
            runner_worktree_root.resolve(strict=False) if runner_worktree_root else None
        )
        self.verification_runner_poll_seconds = verification_runner_poll_seconds
        self.execution_deadline: float | None = None
        self.edited_paths: list[Path] = []

    def set_execution_deadline(self, deadline: float) -> None:
        self.execution_deadline = deadline

    def run(self, argv: list[str], phase: Phase) -> CommandResult:
        normalized, is_verification = self._validate_command(argv, phase)
        configured_timeout = (
            self.verification_timeout_seconds if is_verification else self.timeout_seconds
        )
        timeout = self._effective_timeout(configured_timeout)
        if timeout <= 0:
            return CommandResult(
                argv=tuple(normalized),
                return_code=124,
                output="[Job execution deadline reached before command start]",
                timed_out=True,
                is_verification=is_verification,
            )
        if is_verification and self.verification_backend == "runner":
            return self._run_via_runner(normalized, timeout)
        if is_verification and self.verification_backend == "docker":
            command = self._docker_command(normalized)
            reported_argv = normalized
        else:
            command = self._host_command(normalized)
            reported_argv = command
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=self._safe_environment(),
            )
            combined = self._combine_output(completed.stdout, completed.stderr)
            return CommandResult(
                argv=tuple(reported_argv),
                return_code=completed.returncode,
                output=truncate_output(
                    combined,
                    max_chars=self.max_output_chars,
                    max_lines=self.max_output_lines,
                ),
                is_verification=is_verification,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            combined = self._combine_output(stdout, stderr)
            return CommandResult(
                argv=tuple(reported_argv),
                return_code=124,
                output=truncate_output(
                    f"{combined}\n[Command timed out after {timeout}s]",
                    max_chars=self.max_output_chars,
                    max_lines=self.max_output_lines,
                ),
                timed_out=True,
                is_verification=is_verification,
            )
        except FileNotFoundError as exc:
            return CommandResult(
                argv=tuple(reported_argv),
                return_code=127,
                output=f"[Verification runtime unavailable: {Path(exc.filename or '').name}]",
                is_verification=is_verification,
            )

    def apply_edits(self, edits: Iterable[FileEdit]) -> list[Path]:
        """Validate every edit first, then write; failed batches make no changes."""

        staged: dict[Path, str] = {}
        originals: dict[Path, str | None] = {}
        for edit in edits:
            path = self._safe_edit_path(edit.path)
            if path not in originals:
                originals[path] = path.read_text(encoding="utf-8") if path.exists() else None
                staged[path] = originals[path] or ""

            current = staged[path]
            if edit.mode == "create":
                if edit.search != "":
                    raise EditError(f"create mode requires an empty search: {edit.path}")
                if originals[path] is not None or current:
                    raise EditError(f"create mode requires a new file: {edit.path}")
                updated = edit.replace
            elif edit.mode == "append":
                if edit.search != "":
                    raise EditError(f"append mode requires an empty search: {edit.path}")
                if originals[path] is None:
                    raise EditError(f"append mode requires an existing file: {edit.path}")
                updated = current + edit.replace
            else:
                if edit.search == "":
                    raise EditError(f"replace mode requires a non-empty search: {edit.path}")
                occurrences = current.count(edit.search)
                if occurrences != 1:
                    raise EditError(
                        f"search text must occur exactly once in {edit.path}; found {occurrences}"
                    )
                updated = current.replace(edit.search, edit.replace, 1)

            if len(updated.encode("utf-8")) > 200_000:
                raise EditError(f"edited file exceeds the 200 KB limit: {edit.path}")
            staged[path] = updated

        changed: list[Path] = []
        for path, content in staged.items():
            if originals[path] == content:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            relative = path.relative_to(self.root)
            changed.append(relative)
            if relative not in self.edited_paths:
                self.edited_paths.append(relative)
        return changed

    def has_changes(self) -> bool:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
            env=self._safe_environment(),
        )
        return bool(completed.stdout.strip())

    def enforce_change_limits(self, *, max_files: int, max_diff_lines: int) -> None:
        paths = list(dict.fromkeys(self.edited_paths))
        if len(paths) > max_files:
            raise ComplexityLimitError(
                f"change touches {len(paths)} files; automatic limit is {max_files}"
            )

        total_lines = 0
        accounted: set[Path] = set()
        if paths:
            completed = subprocess.run(
                ["git", "diff", "--numstat", "--no-renames", "HEAD", "--", *map(str, paths)],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env=self._safe_environment(),
            )
            if completed.returncode != 0:
                raise ComplexityLimitError("could not measure the proposed diff")
            for line in completed.stdout.splitlines():
                added, deleted, raw_path = line.split("\t", 2)
                accounted.add(Path(raw_path))
                if added == "-" or deleted == "-":
                    total_lines = max_diff_lines + 1
                    break
                total_lines += int(added) + int(deleted)

        for path in paths:
            if path in accounted:
                continue
            candidate = self.root / path
            if candidate.exists():
                total_lines += len(candidate.read_text(encoding="utf-8").splitlines())

        if total_lines > max_diff_lines:
            raise ComplexityLimitError(
                f"change contains {total_lines} added/deleted lines; "
                f"automatic limit is {max_diff_lines}"
            )

    def run_fail_to_pass(
        self,
        test_paths: list[Path],
        commands: list[list[str]],
    ) -> list[CommandResult]:
        """Overlay new tests on the base commit and prove at least one gate fails there."""

        if not test_paths:
            raise ComplexityLimitError("fail-to-pass proof requires an edited regression test")
        archive = subprocess.run(
            ["git", "archive", "--format=tar", "HEAD"],
            cwd=self.root,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
            env=self._safe_environment(),
        )
        if archive.returncode != 0:
            raise ComplexityLimitError("could not create a clean baseline snapshot")

        temporary_root: Path | None = None
        if self.verification_backend == "runner":
            if self.runner_worktree_root is None:
                raise ComplexityLimitError("runner backend requires a shared worktree root")
            temporary_root = self.runner_worktree_root / ".baselines"
            temporary_root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(
            prefix="issue-agent-baseline-",
            dir=temporary_root,
        ) as temporary:
            baseline_root = Path(temporary)
            with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as bundle:
                self._extract_regular_files(bundle, baseline_root)
            for relative in test_paths:
                source = self.root / relative
                if not source.is_file():
                    raise ComplexityLimitError(f"regression test is not a file: {relative}")
                destination = baseline_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

            baseline_tools = WorkspaceTools(
                baseline_root,
                timeout_seconds=self.timeout_seconds,
                verification_timeout_seconds=self.verification_timeout_seconds,
                max_output_chars=self.max_output_chars,
                max_output_lines=self.max_output_lines,
                verification_backend=self.verification_backend,
                verification_container_image=self.verification_container_image,
                verification_runner_queue_path=self.verification_runner_queue_path,
                runner_worktree_root=self.runner_worktree_root,
                verification_runner_poll_seconds=self.verification_runner_poll_seconds,
            )
            baseline_tools.execution_deadline = self.execution_deadline
            targeted_commands = self._targeted_baseline_commands(test_paths, commands)
            return [baseline_tools.run(command, "verify") for command in targeted_commands]

    @staticmethod
    def _targeted_baseline_commands(
        test_paths: list[Path], commands: list[list[str]]
    ) -> list[list[str]]:
        paths = [str(path) for path in test_paths]
        targeted: list[list[str]] = []
        for command in commands:
            if not command:
                continue
            executable = Path(command[0]).name
            if executable == "pytest":
                targeted.append([*command, *paths])
            elif executable in {"python", "python3"} and command[1:3] == ["-m", "pytest"]:
                targeted.append([*command, *paths])
            elif executable in {"python", "python3"} and command[1:3] == ["-m", "unittest"]:
                targeted.append([*command[:3], *paths])
            elif executable == "uv" and command[1:3] == ["run", "pytest"]:
                targeted.append([*command, *paths])
            elif executable == "uv" and command[1:5] == ["run", "python", "-m", "pytest"]:
                targeted.append([*command, *paths])
            elif executable == "uv" and command[1:5] == ["run", "python", "-m", "unittest"]:
                targeted.append([*command[:5], *paths])
        if not targeted:
            raise ComplexityLimitError(
                "fail-to-pass requires a pytest or unittest verification command"
            )
        return targeted

    def _run_via_runner(self, argv: list[str], timeout: int) -> CommandResult:
        if self.runner_worktree_root is None:
            raise ToolPolicyError("runner backend requires a shared worktree root")
        if not self.root.is_relative_to(self.runner_worktree_root):
            raise ToolPolicyError("worktree is outside the shared runner root")

        request_id = uuid.uuid4().hex
        request_dir = self.verification_runner_queue_path / "requests"
        response_dir = self.verification_runner_queue_path / "responses"
        request_dir.mkdir(parents=True, exist_ok=True)
        response_dir.mkdir(parents=True, exist_ok=True)
        request_path = request_dir / f"{request_id}.json"
        response_path = response_dir / f"{request_id}.json"
        temporary_path = request_dir / f".{request_id}.tmp"
        payload = {
            "id": request_id,
            "workspace": str(self.root.relative_to(self.runner_worktree_root)),
            "argv": argv,
            "timeout_seconds": timeout,
        }
        temporary_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary_path, request_path)

        deadline = time.monotonic() + timeout + 1
        try:
            while time.monotonic() < deadline:
                if response_path.exists():
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                    return CommandResult(
                        argv=tuple(argv),
                        return_code=int(response["return_code"]),
                        output=truncate_output(
                            str(response["output"]),
                            max_chars=self.max_output_chars,
                            max_lines=self.max_output_lines,
                        ),
                        timed_out=bool(response.get("timed_out", False)),
                        is_verification=True,
                    )
                time.sleep(self.verification_runner_poll_seconds)
        finally:
            request_path.unlink(missing_ok=True)
            response_path.unlink(missing_ok=True)
            temporary_path.unlink(missing_ok=True)

        return CommandResult(
            argv=tuple(argv),
            return_code=124,
            output="[Verification runner did not respond before the deadline]",
            timed_out=True,
            is_verification=True,
        )

    def _effective_timeout(self, configured_timeout: int) -> int:
        if self.execution_deadline is None:
            return configured_timeout
        remaining = int(self.execution_deadline - time.monotonic() - 1)
        return max(0, min(configured_timeout, remaining))

    @staticmethod
    def _extract_regular_files(bundle: tarfile.TarFile, destination_root: Path) -> None:
        resolved_root = destination_root.resolve(strict=True)
        for member in bundle.getmembers():
            if not member.isfile():
                continue
            destination = (resolved_root / member.name).resolve(strict=False)
            if not destination.is_relative_to(resolved_root):
                raise ComplexityLimitError("baseline archive contains an unsafe path")
            source = bundle.extractfile(member)
            if source is None:
                raise ComplexityLimitError("baseline archive contains an unreadable file")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as output:
                shutil.copyfileobj(source, output)

    def _validate_command(self, argv: list[str], phase: Phase) -> tuple[list[str], bool]:
        if not argv or len(argv) > 30 or any(not isinstance(item, str) for item in argv):
            raise ToolPolicyError("command must be a non-empty string array of at most 30 items")
        if any("\x00" in item or "\n" in item or "\r" in item for item in argv):
            raise ToolPolicyError("command arguments cannot contain control characters")

        self._validate_path_arguments(argv[1:])
        executable = Path(argv[0]).name
        if executable != argv[0]:
            raise ToolPolicyError("command executable must be resolved from the server PATH")

        if executable in _READ_COMMANDS:
            self._validate_read_command(argv)
            return argv, False
        if executable == "find":
            denied = {
                "-H",
                "-L",
                "-delete",
                "-exec",
                "-execdir",
                "-fls",
                "-fprint",
                "-fprint0",
                "-ok",
            }
            if denied.intersection(argv):
                raise ToolPolicyError("mutating find options are not allowed")
            return argv, False
        if executable == "git":
            if len(argv) < 2 or argv[1] not in _GIT_READ_SUBCOMMANDS:
                raise ToolPolicyError("only read-only git subcommands are allowed")
            denied_git_options = {
                "--ext-diff",
                "--open-files-in-pager",
                "--textconv",
            }
            if denied_git_options.intersection(argv) or any(
                item.startswith("--output") for item in argv
            ):
                raise ToolPolicyError("git output hooks and output files are not allowed")
            return argv, False
        if phase == "diagnose":
            raise ToolPolicyError("diagnose phase only permits read-only commands")

        if executable == "ruff":
            self._validate_ruff(argv)
            return ["python", "-m", "ruff", *argv[1:]], True
        if executable == "pytest":
            return ["python", "-m", "pytest", *argv[1:]], True
        if executable in {"python", "python3"}:
            if len(argv) >= 3 and argv[1:3] == ["-m", "ruff"]:
                self._validate_ruff(["ruff", *argv[3:]])
                return ["python", *argv[1:]], True
            if len(argv) >= 3 and argv[1:3] in (
                ["-m", "pytest"],
                ["-m", "unittest"],
                ["-m", "compileall"],
            ):
                return ["python", *argv[1:]], True
            raise ToolPolicyError("python may only run pytest, unittest, or compileall modules")
        if executable == "uv":
            if len(argv) < 3 or argv[1] != "run":
                raise ToolPolicyError("uv may only be used as `uv run <verification>`")
            nested = Path(argv[2]).name
            if nested == "ruff":
                self._validate_ruff(argv[2:])
                return argv, True
            if nested == "pytest":
                return argv, True
            if nested in {"python", "python3"} and argv[3:5] in (
                ["-m", "pytest"],
                ["-m", "unittest"],
                ["-m", "compileall"],
            ):
                return argv, True
            raise ToolPolicyError("uv run may only execute pytest, ruff, or compileall")
        raise ToolPolicyError(f"command is not allowed: {executable}")

    @staticmethod
    def _host_command(argv: list[str]) -> list[str]:
        if argv and argv[0] == "python":
            return [sys.executable, *argv[1:]]
        return argv

    def _docker_command(self, argv: list[str]) -> list[str]:
        if self.verification_backend != "docker":
            raise ToolPolicyError("unsupported verification backend")
        image_pattern = r"[A-Za-z0-9][A-Za-z0-9._/@:-]{0,200}"
        if re.fullmatch(image_pattern, self.verification_container_image) is None:
            raise ToolPolicyError("invalid verification container image")
        return [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--memory=512m",
            "--cpus=1.0",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTEST_ADDOPTS=-p no:cacheprovider",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m",
            "--mount",
            f"type=bind,src={self.root},dst=/workspace,ro",
            "--workdir",
            "/workspace",
            self.verification_container_image,
            *argv,
        ]

    @staticmethod
    def _validate_read_command(argv: list[str]) -> None:
        executable = argv[0]
        if executable == "sed":
            if len(argv) < 4 or argv[1] != "-n":
                raise ToolPolicyError("sed is limited to `sed -n <line-range>p <file>`")
            if re.fullmatch(r"\d+(,\d+)?p", argv[2]) is None:
                raise ToolPolicyError("sed script may only print a numeric line or line range")
        if executable == "rg":
            denied_rg_options = {"--follow", "--hostname-bin", "--pre", "--pre-glob"}
            if denied_rg_options.intersection(argv) or any(
                item.startswith("--pre=") or item.startswith("--hostname-bin=") for item in argv
            ):
                raise ToolPolicyError("ripgrep preprocessors and symlink following are not allowed")

    @staticmethod
    def _validate_ruff(argv: list[str]) -> None:
        if len(argv) < 2 or argv[1] != "check":
            raise ToolPolicyError("ruff is limited to non-mutating `ruff check`")
        if any(
            item == "--fix"
            or item.startswith("--fix=")
            or item == "--output-file"
            or item.startswith("--output-file=")
            for item in argv
        ):
            raise ToolPolicyError("ruff auto-fix and output files are not allowed")

    def _validate_path_arguments(self, arguments: Iterable[str]) -> None:
        for argument in arguments:
            lowered = argument.lower()
            argument_name = Path(argument).name.lower()
            if (
                argument_name in _DENIED_EDIT_NAMES
                or argument_name.startswith(".env.")
                or Path(argument).suffix.lower() in _DENIED_EDIT_SUFFIXES
            ):
                raise ToolPolicyError("commands cannot access credential-like paths")
            if lowered in {"--follow", "--dereference"}:
                raise ToolPolicyError("commands cannot follow symbolic links")
            if argument.startswith("@"):
                raise ToolPolicyError("command response files are not allowed")
            path_fragments = [argument]
            if "=" in argument:
                path_fragments.append(argument.split("=", 1)[1])
            for fragment in path_fragments:
                if fragment == ".." or fragment.startswith("../") or "/../" in fragment:
                    raise ToolPolicyError("parent-directory traversal is not allowed")
                fragment_path = Path(fragment)
                if fragment_path.is_absolute() and not fragment_path.resolve(
                    strict=False
                ).is_relative_to(self.root):
                    raise ToolPolicyError("absolute paths outside the worktree are not allowed")
            candidate = Path(argument)
            local_candidate = self.root / candidate
            if (
                not candidate.is_absolute()
                and local_candidate.exists()
                and not local_candidate.resolve().is_relative_to(self.root)
            ):
                raise ToolPolicyError("command path resolves outside the worktree")

    def _safe_edit_path(self, raw_path: str) -> Path:
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ToolPolicyError("edit path must stay inside the worktree")
        lowered_parts = {part.lower() for part in relative.parts}
        lowered_relative = Path(*(part.lower() for part in relative.parts))
        name = relative.name.lower()
        if ".git" in lowered_parts or name in _DENIED_EDIT_NAMES or name.startswith(".env."):
            raise ToolPolicyError(f"sensitive path cannot be edited: {raw_path}")
        if relative.suffix.lower() in _DENIED_EDIT_SUFFIXES:
            raise ToolPolicyError(f"credential-like file cannot be edited: {raw_path}")
        if any(
            lowered_relative == denied or denied in lowered_relative.parents
            for denied in _DENIED_AUTOMATION_PATHS
        ):
            raise ToolPolicyError(f"automation definitions cannot be edited: {raw_path}")

        candidate = (self.root / relative).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise ToolPolicyError("resolved edit path leaves the worktree")
        return candidate

    @staticmethod
    def _combine_output(stdout: str, stderr: str) -> str:
        parts = []
        if stdout:
            parts.append(f"STDOUT:\n{stdout.rstrip()}")
        if stderr:
            parts.append(f"STDERR:\n{stderr.rstrip()}")
        return "\n".join(parts) if parts else "[no output]"

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        allowed = {"LANG", "LC_ALL", "PATH", "PYTHONPATH", "TERM", "TMPDIR", "VIRTUAL_ENV"}
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
        return environment

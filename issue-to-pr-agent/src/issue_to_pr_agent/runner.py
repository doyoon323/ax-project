from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
_DENIED_NAMES = {".env", "credentials", "id_ed25519", "id_rsa"}
_DENIED_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}


def _write_response(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


def _validate_paths(arguments: list[str], workspace: Path) -> None:
    for argument in arguments:
        name = Path(argument).name.lower()
        if (
            name in _DENIED_NAMES
            or name.startswith(".env.")
            or Path(argument).suffix.lower() in _DENIED_SUFFIXES
        ):
            raise ValueError("credential-like path is forbidden")
        for fragment in (argument, argument.split("=", 1)[-1]):
            if fragment == ".." or fragment.startswith("../") or "/../" in fragment:
                raise ValueError("parent path traversal is forbidden")
            path = Path(fragment)
            if path.is_absolute() and not path.resolve(strict=False).is_relative_to(workspace):
                raise ValueError("absolute path leaves the workspace")


def _validate_ruff(argv: list[str]) -> None:
    if len(argv) < 2 or argv[1] != "check":
        raise ValueError("ruff is limited to check mode")
    if any(item == "--fix" or item.startswith("--fix=") for item in argv):
        raise ValueError("ruff fixes are forbidden")


def _verification_command(argv: Any, workspace: Path) -> list[str]:
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > 30
        or any(not isinstance(item, str) for item in argv)
    ):
        raise ValueError("argv must be a non-empty string list")
    if any("\x00" in item or "\n" in item or "\r" in item for item in argv):
        raise ValueError("control characters are forbidden")
    _validate_paths(argv[1:], workspace)
    executable = Path(argv[0]).name
    if executable != argv[0]:
        raise ValueError("executable must come from PATH")

    if executable == "ruff":
        _validate_ruff(argv)
        return [sys.executable, "-m", "ruff", *argv[1:]]
    if executable == "pytest":
        return [sys.executable, "-m", "pytest", *argv[1:]]
    if executable in {"python", "python3"}:
        if (
            len(argv) < 3
            or argv[1] != "-m"
            or argv[2]
            not in {
                "compileall",
                "pytest",
                "ruff",
                "unittest",
            }
        ):
            raise ValueError("python module is not an allowed verifier")
        if argv[2] == "ruff":
            _validate_ruff(["ruff", *argv[3:]])
        return [sys.executable, *argv[1:]]
    if executable == "uv":
        if len(argv) < 3 or argv[1] != "run":
            raise ValueError("uv is limited to verification commands")
        nested = Path(argv[2]).name
        if nested == "ruff":
            _validate_ruff(argv[2:])
        elif nested == "pytest":
            pass
        elif nested in {"python", "python3"}:
            if (
                len(argv) < 5
                or argv[3] != "-m"
                or argv[4]
                not in {
                    "compileall",
                    "pytest",
                    "unittest",
                }
            ):
                raise ValueError("uv python module is not an allowed verifier")
        else:
            raise ValueError("uv command is not an allowed verifier")
        return argv
    raise ValueError("runner accepts verification commands only")


def _run_command(
    argv: list[str],
    workspace: Path,
    timeout: int,
    heartbeat: Path | None = None,
) -> dict[str, Any]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"LANG", "LC_ALL", "PATH", "TERM", "TMPDIR"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPYCACHEPREFIX"] = "/tmp/pycache"
    environment["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    if heartbeat is not None:

        def maintain_heartbeat() -> None:
            while not heartbeat_stop.is_set():
                heartbeat.touch()
                heartbeat_stop.wait(2)

        heartbeat_thread = threading.Thread(target=maintain_heartbeat, daemon=True)
        heartbeat_thread.start()
    try:
        try:
            process = subprocess.Popen(
                argv,
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                timed_out = False
                return_code = process.returncode
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
                timed_out = True
                return_code = 124
        except FileNotFoundError:
            return {
                "return_code": 127,
                "output": "[Verification runtime unavailable]",
                "timed_out": False,
            }
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1)

    output = (
        "\n".join(
            part
            for part in (
                f"STDOUT:\n{stdout.rstrip()}" if stdout else "",
                f"STDERR:\n{stderr.rstrip()}" if stderr else "",
            )
            if part
        )
        or "[no output]"
    )
    if timed_out:
        output = f"{output}\n[Command timed out after {timeout}s]"
    lines = output.splitlines()
    if len(lines) > 50:
        output = "\n".join([*lines[:35], "[...Output Truncated...]", *lines[-14:]])
    if len(output) > 1_000:
        output = f"{output[:690]}\n[...Output Truncated...]\n{output[-270:]}"
    return {"return_code": return_code, "output": output, "timed_out": timed_out}


def process_request(
    request_path: Path,
    response_dir: Path,
    worktree_root: Path,
    heartbeat: Path | None = None,
) -> None:
    response: dict[str, Any]
    request_id = request_path.stem
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if request.get("id") != request_id or not _REQUEST_ID.fullmatch(request_id):
            raise ValueError("invalid request id")
        relative = Path(str(request["workspace"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("workspace must be relative")
        workspace = (worktree_root / relative).resolve(strict=True)
        if not workspace.is_relative_to(worktree_root):
            raise ValueError("workspace leaves the shared root")
        timeout = int(request["timeout_seconds"])
        if not 1 <= timeout <= 900:
            raise ValueError("invalid verification timeout")
        command = _verification_command(request["argv"], workspace)
        request_path.unlink(missing_ok=True)
        response = _run_command(command, workspace, timeout, heartbeat)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        response = {
            "return_code": 126,
            "output": f"[Runner rejected request: {type(exc).__name__}]",
            "timed_out": False,
        }
    finally:
        request_path.unlink(missing_ok=True)
    _write_response(response_dir / f"{request_id}.json", response)


def run(queue_path: Path, worktree_root: Path, poll_seconds: float) -> None:
    worktree_root = worktree_root.resolve(strict=True)
    request_dir = queue_path / "requests"
    response_dir = queue_path / "responses"
    request_dir.mkdir(parents=True, exist_ok=True)
    response_dir.mkdir(parents=True, exist_ok=True)
    heartbeat = queue_path / "runner-heartbeat"
    last_heartbeat = 0.0
    while True:
        now = time.time()
        if now - last_heartbeat >= 2:
            heartbeat.touch()
            last_heartbeat = now
        requests = sorted(request_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
        if not requests:
            time.sleep(poll_seconds)
            continue
        process_request(requests[0], response_dir, worktree_root, heartbeat)


def main() -> None:
    parser = argparse.ArgumentParser(description="Unprivileged issue-agent verification runner")
    parser.add_argument("--queue", type=Path, default=Path("/runner-queue"))
    parser.add_argument("--worktrees", type=Path, default=Path("/worktrees"))
    parser.add_argument("--poll-seconds", type=float, default=0.1)
    arguments = parser.parse_args()
    run(arguments.queue, arguments.worktrees, arguments.poll_seconds)


if __name__ == "__main__":
    main()

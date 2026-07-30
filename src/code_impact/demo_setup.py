"""Create a small Git repository used by the Code Impact Agent demo."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def _run_git(repo_path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _commit(repo_path: Path, message: str, timestamp: str) -> str:
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = timestamp
    env["GIT_COMMITTER_DATE"] = timestamp
    _run_git(repo_path, "add", ".")
    _run_git(repo_path, "commit", "--quiet", "-m", message, env=env)
    return _run_git(repo_path, "rev-parse", "HEAD")


def create_demo_repository(target: Path, template: Path | None = None) -> dict[str, str]:
    """Create the AV simulation repository and return its demo commit IDs."""
    target = target.resolve()
    if target.exists():
        raise FileExistsError(f"Demo repository already exists: {target}")

    project_root = Path(__file__).resolve().parents[2]
    template = template or project_root / "demo" / "av-sim"
    shutil.copytree(template, target)

    _run_git(target, "init", "--quiet", "-b", "main")
    _run_git(target, "config", "user.name", "Code Impact Demo")
    _run_git(target, "config", "user.email", "demo@example.com")

    baseline = _commit(
        target,
        "chore: create av simulation baseline",
        "2026-07-30T09:00:00+09:00",
    )
    _run_git(target, "tag", "demo-baseline", baseline)

    safety_file = target / "control" / "safety_check.py"
    before = safety_file.read_text(encoding="utf-8")
    old_rule = "    return max(3.0, speed_mps * 0.8)\n"
    new_rule = (
        "    reaction_distance = speed_mps * 1.2\n    return max(5.0, reaction_distance + 2.0)\n"
    )
    if old_rule not in before:
        raise RuntimeError("The demo safety rule was not found in the template.")
    safety_file.write_text(before.replace(old_rule, new_rule), encoding="utf-8")

    safety_change = _commit(
        target,
        "fix: increase autonomous braking safety distance",
        "2026-07-30T09:05:00+09:00",
    )
    _run_git(target, "tag", "demo-safety-change", safety_change)

    readme_file = target / "README.md"
    readme_file.write_text(
        readme_file.read_text(encoding="utf-8") + "\n## Units\n\nAll distance inputs use meters.\n",
        encoding="utf-8",
    )
    docs_only = _commit(
        target,
        "docs: document sensor distance units",
        "2026-07-30T09:10:00+09:00",
    )
    _run_git(target, "tag", "demo-docs-only", docs_only)

    return {
        "baseline": baseline,
        "safety_change": safety_change,
        "docs_only": docs_only,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("demo/work/av-sim"),
        help="Path where the demo Git repository will be created.",
    )
    args = parser.parse_args()

    commits = create_demo_repository(args.output)
    print(json.dumps(commits, indent=2))


if __name__ == "__main__":
    main()

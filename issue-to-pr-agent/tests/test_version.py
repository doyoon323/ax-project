from __future__ import annotations

import tomllib
from pathlib import Path

from issue_to_pr_agent import __version__


def test_package_version_matches_project_metadata() -> None:
    project_file = Path(__file__).parents[1] / "pyproject.toml"
    metadata = tomllib.loads(project_file.read_text(encoding="utf-8"))

    assert __version__ == metadata["project"]["version"]

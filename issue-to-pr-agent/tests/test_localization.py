from __future__ import annotations

import subprocess
from pathlib import Path

from issue_to_pr_agent.localization import RepositoryLocalizer
from issue_to_pr_agent.models import IssueTask


def make_issue(*, title: str, body: str) -> IssueTask:
    return IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=17,
        title=title,
        body=body,
        author="octocat",
        author_association="OWNER",
    )


def initialize_repository(root: Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)


def test_localizer_ranks_exact_match_and_extracts_python_skeleton(tmp_path: Path) -> None:
    initialize_repository(
        tmp_path,
        {
            "src/calculator.py": (
                "class Calculator:\n"
                "    def divide(self, left, right):\n"
                "        return left / right\n"
            ),
            "src/unrelated.py": "def greet():\n    return 'hello'\n",
            "tests/test_calculator.py": "def test_addition():\n    assert 1 + 1 == 2\n",
            "README.md": "A small calculator project.\n",
        },
    )
    (tmp_path / ".env").write_text("TOKEN=do-not-read\n", encoding="utf-8")

    result = RepositoryLocalizer(tmp_path).localize(
        make_issue(
            title="Calculator.divide returns ZeroDivisionError",
            body="Update `Calculator.divide` so dividing by zero returns None.",
        )
    )

    assert result.candidate_paths[0] == "src/calculator.py"
    assert "calculator.divide" in result.terms
    assert any("class Calculator" in item for item in result.candidates[0].declarations)
    assert any("def divide" in item for item in result.candidates[0].declarations)
    assert result.scanned_files == 4
    rendered = result.render(max_tree_entries=200, max_chars=12_000)
    assert "<UNTRUSTED_LOCALIZATION_CONTEXT>" in rendered
    assert ".env" not in rendered


def test_localizer_bounds_candidates_and_context(tmp_path: Path) -> None:
    files = {
        f"src/module_{index}.py": f"def target_{index}():\n    return 'needle'\n"
        for index in range(20)
    }
    initialize_repository(tmp_path, files)

    result = RepositoryLocalizer(tmp_path, max_candidates=3).localize(
        make_issue(title="Find needle", body="The needle behavior is incorrect.")
    )
    rendered = result.render(max_tree_entries=20, max_chars=400)

    assert len(result.candidates) == 3
    assert len(rendered) <= 400
    assert "[localization context truncated]" in rendered
    assert rendered.endswith("</UNTRUSTED_LOCALIZATION_CONTEXT>")


def test_localizer_ignores_invalid_or_sensitive_paths(tmp_path: Path) -> None:
    initialize_repository(
        tmp_path,
        {
            "src/broken.py": "def broken(:\n",
            "src/valid.py": "async def process_event():\n    return True\n",
            "config/private.pem": "secret\n",
            "node_modules/package.py": "needle = True\n",
        },
    )
    (tmp_path / "outside.py").write_text("external_secret = True\n", encoding="utf-8")
    (tmp_path / "src/external.py").symlink_to(tmp_path / "outside.py")
    subprocess.run(["git", "add", "src/external.py"], cwd=tmp_path, check=True)

    result = RepositoryLocalizer(tmp_path).localize(
        make_issue(title="process_event fails", body="Inspect process_event in valid.py")
    )

    assert "config/private.pem" not in result.repository_paths
    assert "node_modules/package.py" not in result.repository_paths
    assert "src/external.py" not in result.candidate_paths
    assert result.candidate_paths[0] == "src/valid.py"
    assert any("async def process_event" in item for item in result.candidates[0].declarations)


def test_localizer_falls_back_when_git_listing_is_unavailable(
    monkeypatch: object, tmp_path: Path
) -> None:
    (tmp_path / "source.py").write_text("def recover():\n    return True\n", encoding="utf-8")

    def unavailable(*args: object, **kwargs: object) -> object:
        raise OSError("git unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable)
    result = RepositoryLocalizer(tmp_path).localize(
        make_issue(title="recover failure", body="The recover function is broken")
    )

    assert result.candidate_paths == ["source.py"]
    assert any("def recover" in item for item in result.candidates[0].declarations)

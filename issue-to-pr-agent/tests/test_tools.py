from __future__ import annotations

import sys
from pathlib import Path

import pytest

from issue_to_pr_agent.models import FileEdit
from issue_to_pr_agent.tools import (
    ComplexityLimitError,
    ToolPolicyError,
    WorkspaceTools,
    truncate_output,
)


def test_truncate_output_keeps_within_limits() -> None:
    text = "\n".join(f"line-{index}-" + ("x" * 30) for index in range(100))
    result = truncate_output(text, max_chars=300, max_lines=12)

    assert len(result) <= 300
    assert "Output Truncated" in result
    assert "line-0" in result
    assert "line-99" in result


def test_structured_edit_and_command_policy(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    tools = WorkspaceTools(tmp_path)

    changed = tools.apply_edits(
        [FileEdit(path="sample.py", search="value = 1", replace="value = 2")]
    )

    assert changed == [Path("sample.py")]
    assert source.read_text(encoding="utf-8") == "value = 2\n"
    with pytest.raises(ToolPolicyError):
        tools.run(["bash", "-lc", "echo unsafe"], "patch")
    with pytest.raises(ToolPolicyError):
        tools.apply_edits([FileEdit(path="../outside.py", search="", replace="bad")])
    with pytest.raises(ToolPolicyError):
        tools.run(["sed", "-n", "1,10p", ".env"], "diagnose")
    with pytest.raises(ToolPolicyError):
        tools.run(["sed", "-i", "s/one/two/", "sample.py"], "patch")
    with pytest.raises(ToolPolicyError):
        tools.run(["rg", "--pre", "cat", "value"], "diagnose")
    with pytest.raises(ToolPolicyError):
        tools.run(["git", "diff", "--output=diff.txt"], "verify")
    with pytest.raises(ToolPolicyError):
        tools.run(["ruff", "check", "--fix", "."], "verify")
    with pytest.raises(ToolPolicyError):
        tools.apply_edits([FileEdit(path=".github/workflows/agent.yml", search="", replace="bad")])


def test_append_mode_adds_to_existing_file(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    tools = WorkspaceTools(tmp_path)

    changed = tools.apply_edits(
        [
            FileEdit(
                mode="append",
                path="sample.py",
                search="",
                replace="value = 2\n",
            )
        ]
    )

    assert changed == [Path("sample.py")]
    assert source.read_text(encoding="utf-8") == "value = 1\nvalue = 2\n"


def test_verification_command_is_recognized(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(1, 1)\n",
        encoding="utf-8",
    )
    result = WorkspaceTools(tmp_path).run(["python", "-m", "compileall", "sample.py"], "patch")

    assert result.succeeded
    assert result.is_verification
    assert result.argv[0] == sys.executable

    unittest_result = WorkspaceTools(tmp_path).run(
        ["python", "-m", "unittest", "discover", "-s", "tests"], "verify"
    )
    assert unittest_result.succeeded
    assert unittest_result.is_verification
    assert unittest_result.argv[0] == sys.executable


def test_docker_verification_is_networkless_and_resource_limited(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, verification_backend="docker")

    command = tools._docker_command(["python", "-m", "unittest", "discover", "-v"])

    assert command[:3] == ["docker", "run", "--rm"]
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--pull=never" in command
    assert f"type=bind,src={tmp_path.resolve()},dst=/workspace,ro" in command


def test_complexity_limit_and_fail_to_pass_proof(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "calculator.py").write_text(
        "def add(left, right):\n    return left - right\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "calculator.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    tools = WorkspaceTools(tmp_path)
    tools.apply_edits(
        [
            FileEdit(
                path="calculator.py",
                search="return left - right",
                replace="return left + right",
            ),
            FileEdit(
                mode="create",
                path="tests/test_calculator.py",
                search="",
                replace=(
                    "import unittest\nfrom calculator import add\n\n"
                    "class AddTest(unittest.TestCase):\n"
                    "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"
                ),
            ),
        ]
    )

    with pytest.raises(ComplexityLimitError, match="touches 2 files"):
        tools.enforce_change_limits(max_files=1, max_diff_lines=100)
    baseline = tools.run_fail_to_pass(
        [Path("tests/test_calculator.py")],
        [["python", "-m", "unittest", "discover", "-s", "tests"]],
    )

    assert any(not result.succeeded for result in baseline)
    assert tools.run(["python", "-m", "unittest", "discover", "-s", "tests"], "verify").succeeded


def test_fail_to_pass_rejects_import_error_on_baseline(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    (tmp_path / "new_feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "test_new_feature.py").write_text(
        "import unittest\n"
        "from new_feature import VALUE\n\n"
        "class NewFeatureTest(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(VALUE, 2)\n",
        encoding="utf-8",
    )

    with pytest.raises(ComplexityLimitError, match="import, collection, or configuration"):
        WorkspaceTools(tmp_path).run_fail_to_pass(
            [Path("test_new_feature.py")],
            [["python", "-m", "unittest", "discover"]],
        )

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from issue_to_pr_agent.models import FileEdit
from issue_to_pr_agent.tools import ToolPolicyError, WorkspaceTools, truncate_output


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

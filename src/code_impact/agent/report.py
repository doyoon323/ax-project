"""Render a compact, evidence-backed code impact report."""

from __future__ import annotations

from code_impact.agent.results import Evidence, ToolResult


def _status(result: ToolResult) -> str:
    if not result.truncated:
        return str(result.total_count)
    return f"{len(result.items)}개 표시 / 총 {result.total_count}개"


def render_report(
    commit: str | None,
    changed: ToolResult | None,
    impacts: ToolResult | None,
    tests: ToolResult | None,
    evidence: list[Evidence],
    summary: str | None = None,
    summary_evidence_ids: list[str] | None = None,
    warning: str | None = None,
    error: str | None = None,
) -> str:
    lines = ["# Code Impact Report", ""]
    if error:
        lines.extend(["분석할 수 없습니다.", "", f"- 이유: {error}"])
        return "\n".join(lines)

    lines.extend([f"- 분석 커밋: `{commit}`", "- 영향 탐색 범위: 최대 2단계", ""])
    changed = changed or ToolResult([], [], 0, False)
    impacts = impacts or ToolResult([], [], 0, False)
    tests = tests or ToolResult([], [], 0, False)

    if not changed.items:
        lines.extend(
            [
                "## 결과",
                "",
                "Python 함수 범위에 해당하는 변경을 찾지 못했습니다.",
                "근거가 없으므로 영향 함수와 테스트를 추측하지 않습니다.",
            ]
        )
        return "\n".join(lines)

    if summary:
        lines.extend(["## 분석 요약", "", summary])
        if summary_evidence_ids:
            cited = ", ".join(f"`{evidence_id}`" for evidence_id in summary_evidence_ids)
            lines.extend(["", f"- 인용 근거: {cited}"])
        lines.append("")

    lines.extend(["## 변경 함수", "", f"조회 결과: {_status(changed)}"])
    for item in changed.items:
        function = item.function
        lines.append(
            f"- `{function.function_id}` "
            f"— `{function.file_path}:{function.start_line}-{function.end_line}`"
        )

    lines.extend(["", "## 영향 가능 함수", "", f"조회 결과: {_status(impacts)}"])
    if impacts.items:
        for item in impacts.items:
            function = item.function
            lines.append(
                f"- `{function.function_id}` ({item.depth}단계) "
                f"— `{function.file_path}:{function.start_line}-{function.end_line}`"
            )
    else:
        lines.append("- 최대 2단계 안에서 호출자를 찾지 못했습니다.")

    lines.extend(["", "## 권장 테스트", "", f"조회 결과: {_status(tests)}"])
    if tests.items:
        for item in tests.items:
            function = item.function
            lines.append(
                f"- `{function.function_id}` — `{function.file_path}:{function.start_line}`"
            )
    else:
        lines.append("- 정적으로 연결된 테스트를 찾지 못했습니다.")

    valid_evidence = [item for item in evidence if item.valid]
    invalid_count = len(evidence) - len(valid_evidence)
    lines.extend(["", "## 검증된 근거", ""])
    for item in valid_evidence:
        lines.append(f"- `{item.evidence_id}` `{item.file_path}:{item.line}` — {item.description}")
    if invalid_count:
        lines.append(f"- 검증에 실패한 근거 {invalid_count}개는 결과에서 제외했습니다.")

    lines.extend(
        [
            "",
            "> 영향 가능 함수는 오류 발생을 단정하는 결과가 아니라, "
            "변경 후 함께 확인할 코드 후보입니다.",
        ]
    )
    if warning:
        lines.extend(["", "## Gemini 상태", "", f"- {warning}"])
    return "\n".join(lines)

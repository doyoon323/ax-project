from __future__ import annotations

import pytest

from issue_to_pr_agent.issue_validation import IssueInputError, validate_issue_actionability
from issue_to_pr_agent.models import IssueTask


def issue(body: str) -> IssueTask:
    return IssueTask(
        delivery_id="abcdef12-3456",
        repository="owner/repository",
        number=10,
        title="feat: 계산 이력 추가",
        body=body,
        author="octocat",
        author_association="OWNER",
    )


def test_keyboard_mash_is_rejected_before_llm() -> None:
    with pytest.raises(IssueInputError, match="LLM을 호출하지 않았습니다"):
        validate_issue_actionability(issue("ㅂㅈㅇㅁㄹㄴㅇㄹ1"))


def test_short_generic_request_is_rejected_before_llm() -> None:
    with pytest.raises(IssueInputError, match="공백 제외 40자"):
        validate_issue_actionability(issue("기능 추가해 주세요"))


def test_actionable_korean_issue_is_accepted() -> None:
    validate_issue_actionability(
        issue(
            "calculator.py에 Calculator 클래스를 추가한다. add 성공 결과를 기록하고 "
            "history는 tuple을 반환해야 한다. unittest를 추가하고 전체 테스트를 통과한다."
        )
    )

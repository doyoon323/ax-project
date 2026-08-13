from __future__ import annotations

import re
import unicodedata

from .models import IssueTask


class IssueInputError(ValueError):
    """Raised before any LLM call when an Issue is not actionable."""


_WORD = re.compile(r"[A-Za-z가-힣][A-Za-z0-9_가-힣./()`'-]*")
_MIN_BODY_CHARACTERS = 40
_MIN_WORDS = 5
_MIN_UNIQUE_WORDS = 4


def validate_issue_actionability(issue: IssueTask) -> None:
    """Fail closed on empty, keyboard-mash, or non-actionable Issue bodies."""

    body = unicodedata.normalize("NFKC", issue.body).strip()
    compact = "".join(body.split())
    words = [word.casefold() for word in _WORD.findall(body)]
    unique_words = set(words)
    jamo = sum(
        "\u1100" <= character <= "\u11ff" or "\u3130" <= character <= "\u318f" for character in body
    )
    hangul_syllables = sum("가" <= character <= "힣" for character in body)

    keyboard_mash = jamo >= 4 and jamo > hangul_syllables
    too_short = len(compact) < _MIN_BODY_CHARACTERS
    too_few_words = len(words) < _MIN_WORDS or len(unique_words) < _MIN_UNIQUE_WORDS
    if keyboard_mash or too_short or too_few_words:
        raise IssueInputError(
            "Issue 설명이 부족해 LLM을 호출하지 않았습니다. 본문에 수정 대상, 기대 동작, "
            "확인 조건을 포함해 공백 제외 40자 이상으로 작성해 주세요."
        )

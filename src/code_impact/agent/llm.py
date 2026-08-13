"""Gemini adapter for question interpretation and evidence-grounded summaries."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"


class QuestionIntent(BaseModel):
    """Analysis scope extracted from one natural-language question."""

    model_config = ConfigDict(extra="forbid")

    commit_ref: str = Field(min_length=1)
    include_impacts: bool
    include_tests: bool


class GroundedSummary(BaseModel):
    """Short narrative that may cite only evidence supplied by the tools."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(max_length=20)


class LanguageModel(Protocol):
    """Language-model boundary used by the LangGraph workflow."""

    model_name: str

    def interpret_question(self, question: str) -> QuestionIntent: ...

    def summarize_report(self, context: dict[str, Any]) -> GroundedSummary: ...


class GeminiConfigurationError(RuntimeError):
    """Raised when Gemini cannot be configured from the environment."""


class GeminiResponseError(RuntimeError):
    """Raised when Gemini returns no usable structured result."""


class GeminiLanguageModel:
    """Small Gemini client with structured input and output."""

    def __init__(self, client: Any, model_name: str = DEFAULT_GEMINI_MODEL) -> None:
        self.client = client
        self.model_name = model_name

    @classmethod
    def from_env(cls) -> GeminiLanguageModel:
        """Load .env without exposing the API key to application output."""
        load_dotenv()
        model_name = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        try:
            client = genai.Client()
        except Exception as error:
            raise GeminiConfigurationError(
                "Gemini를 초기화하지 못했습니다. GEMINI_API_KEY 설정을 확인하세요."
            ) from error
        return cls(client, model_name)

    def _structured_response(
        self,
        *,
        system_instruction: str,
        payload: dict[str, Any],
        schema: type[BaseModel],
    ) -> BaseModel:
        try:
            interaction = self.client.interactions.create(
                model=self.model_name,
                system_instruction=system_instruction,
                input=json.dumps(payload, ensure_ascii=False),
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema.model_json_schema(),
                },
            )
        except Exception as error:
            raise GeminiResponseError(
                f"Gemini 요청에 실패했습니다 ({type(error).__name__})."
            ) from error

        output_text = getattr(interaction, "output_text", None)
        if not output_text:
            raise GeminiResponseError("Gemini가 구조화된 응답을 반환하지 않았습니다.")

        try:
            return schema.model_validate_json(output_text)
        except Exception as error:
            raise GeminiResponseError("Gemini 응답 형식이 예상한 스키마와 다릅니다.") from error

    def interpret_question(self, question: str) -> QuestionIntent:
        result = self._structured_response(
            system_instruction=(
                "당신은 Python 코드 변경 영향 분석 Agent의 질문 해석기다. "
                "commit_ref는 질문에 실제로 적힌 Git ref만 그대로 추출하고 절대 만들어내지 마라. "
                "사용자가 영향 범위를 요청하면 include_impacts를 true로, 테스트를 요청하면 "
                "include_tests를 true로 설정하라. 단순히 '분석해줘'처럼 범위가 모호하면 둘 다 "
                "true로 설정하라."
            ),
            payload={"question": question},
            schema=QuestionIntent,
        )
        return QuestionIntent.model_validate(result)

    def summarize_report(self, context: dict[str, Any]) -> GroundedSummary:
        result = self._structured_response(
            system_instruction=(
                "당신은 코드 변경 영향 보고서 작성기다. 입력 JSON에 있는 사실만 사용해 한국어로 "
                "2~3문장의 짧은 요약을 작성하라. 영향 가능 함수가 실제로 오류를 일으킨다고 "
                "단정하지 마라. evidence_ids에는 요약을 뒷받침하는 allowed_evidence_ids 중 "
                "필요한 ID만 넣어라. 파일, 함수, 테스트, 원인을 새로 만들어내지 마라."
            ),
            payload=context,
            schema=GroundedSummary,
        )
        summary = GroundedSummary.model_validate(result)
        allowed_ids = set(context["allowed_evidence_ids"])
        unknown_ids = set(summary.evidence_ids) - allowed_ids
        if unknown_ids:
            raise GeminiResponseError("Gemini가 Tool이 발급하지 않은 근거 ID를 인용했습니다.")
        return summary

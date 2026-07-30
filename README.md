# Code Impact Agent

> Git 커밋의 변경 영향과 다시 실행할 테스트를 코드 근거와 함께 알려주는 Python 코드 분석 Agent

## 프로젝트 목적

코드가 변경되면 개발자는 변경된 함수를 사용하는 코드와 다시 실행해야 할 테스트를 직접 찾아야 합니다. 프로젝트가 커질수록 분석 시간이 길어지고 영향 범위나 테스트가 누락될 수 있습니다.

이 프로젝트의 목적은 다음과 같습니다.

> **소스코드의 함수 호출 관계와 테스트 관계를 데이터로 만들고, Git 변경사항의 영향 범위를 근거와 함께 제공하여 개발자의 분석 시간을 줄이고 테스트 누락을 방지합니다.**

핵심 가치는 **변경 분석 시간 단축과 테스트 누락 방지**입니다.

## 무엇을 만드는가

사용자가 Git 커밋 ID를 입력하면 다음 내용을 보고서로 보여주는 Agent를 만듭니다.

1. 해당 커밋에서 변경된 함수
2. 변경된 함수를 호출하는 영향 가능 함수
3. 다시 실행할 것을 권장하는 테스트
4. 판단에 사용한 파일·함수·라인 근거

```text
커밋 ID 입력
  → Git diff에서 변경 함수 확인
  → 저장된 함수 호출 관계를 최대 2단계까지 탐색
  → 관련 테스트 검색
  → 근거 검증
  → 변경 영향 보고서 생성
```

예를 들어 결제 검증 함수가 변경되었다면 다음과 같이 알려줍니다.

```text
변경 함수
- payment.py::validate_payment

영향 가능 함수
- order.py::complete_order          1단계
- api.py::submit_order              2단계

권장 테스트
- tests/test_payment.py::test_validate_payment
- tests/test_order.py::test_submit_order

근거
- order.py:24에서 validate_payment 호출
- api.py:17에서 complete_order 호출
```

여기서 영향 가능 함수는 오류가 발생한다고 단정하는 결과가 아니라, 변경 후 함께 확인해야 할 코드 후보를 의미합니다.

## 동작 원리

Python AST로 함수 정의와 함수 호출 관계를 추출하고 SQLite에 저장합니다. Git diff의 변경 라인과 함수 위치를 비교해 변경된 함수를 찾은 뒤, 호출 관계를 역방향으로 탐색하여 영향 함수와 관련 테스트를 찾습니다.

- 일반 코드: Git·AST 분석, 관계 검색, 라인 근거 발급
- Gemini: 자연어 질문에서 Git ref와 요청 범위를 구조화하고, 검증된 근거를 요약
- LangGraph Agent: Gemini의 해석에 따라 Tool을 선택하고 전체 분석 흐름을 제어

Gemini는 함수 관계를 계산하지 않습니다. 영향 함수와 테스트는 AST·Git·SQLite Tool이
결정하며, Gemini가 보고서에서 인용할 수 있는 근거 ID도 Tool이 발급한 값으로 제한합니다.

구현 코드도 두 역할로 구분합니다.

```text
src/code_impact/db_builder/   소스코드 관계 DB 구축
src/code_impact/agent/        DB 조회와 영향 보고서 생성
```

## 현재 Demo

`av-sim` 샘플 저장소에는 인식, 경로 계획, 판단, 제어와 안전 검증 코드가 포함됩니다. Demo 생성 명령은 다음 세 Git 시점을 만듭니다.

- `demo-baseline`: 최초 코드
- `demo-safety-change`: 안전거리 계산 함수 변경
- `demo-docs-only`: 문서만 변경한 반례

Demo 파일은 역할에 따라 구분됩니다.

```text
demo/av-sim/                 Git에 포함되는 샘플 원본
demo/work/av-sim/            실행 시 생성되는 분석 대상 Git 저장소
demo/data/code-impact.db     실행 시 생성되는 관계 DB
```

`demo/work/`와 `demo/data/`는 실행 결과이므로 Git에 포함하지 않습니다.

Dev Container에서 다음과 같이 실행합니다.

```bash
uv sync
uv run code-impact setup-demo
uv run code-impact analyze \
  "demo-safety-change 커밋의 영향 범위와 테스트를 알려줘."
```

실행 전 프로젝트 루트의 `.env`에 Gemini API 키를 설정합니다. `.env`는 Git에서 제외됩니다.

```dotenv
GEMINI_API_KEY=발급받은_API_키
# 선택 사항
GEMINI_MODEL=gemini-3.6-flash
```

Agent는 질문 하나를 받아 Gemini가 요청 범위를 해석한 뒤 필요한 Tool만 실행합니다.

```text
자연어 질문
  → Gemini가 Git ref·요청 범위 구조화
  → 변경 함수 조회
  → 필요한 경우 최대 2단계 호출자·관련 테스트 조회
  → 파일·라인 근거 검증
  → Gemini가 검증된 근거 ID로 요약
  → 변경 영향 보고서
```

문서 변경 반례도 확인할 수 있습니다.

```bash
uv run code-impact analyze \
  "demo-docs-only 커밋의 영향 범위와 테스트를 알려줘."
```

현재 Demo는 Gemini의 구조화 응답으로 질문을 해석합니다. 예를 들어 “변경 함수만 알려줘”라고
질문하면 호출자와 테스트 Tool을 생략하고, “영향 범위와 테스트를 알려줘”라고 질문하면 두
Tool을 모두 실행합니다.

## 3주 최종 목표

> **소규모 Python 저장소의 커밋 ID를 입력하면 변경 함수, 최대 2단계의 영향 함수, 권장 테스트를 파일·함수·라인 근거와 함께 보여주는 단일 Agent를 구현하고, 12~15개의 변경 커밋으로 결과를 평가합니다.**

| 주차 | 목표 |
|---|---|
| 1주차 | 샘플 프로젝트 제작, Git diff·AST 분석, SQLite 관계 저장 |
| 2주차 | 변경 함수·호출자·관련 테스트 조회 Tool과 LangGraph Agent 구현 |
| 3주차 | 평가용 커밋 완성, 정확도 평가, Streamlit 화면과 Docker 실행 환경 구성 |

## 구현 범위

- Python 샘플 저장소 1개
- 인식·경로 계획·판단·제어로 구성된 `av-sim` 코드
- 평가용 변경 커밋 12~15개
- 함수 호출 관계 최대 2단계
- 커밋 ID 기반 변경 영향 분석
- 함수명 기반 관계 조회
- SQLite, LangGraph, Streamlit, Docker

다음 내용은 구현 범위에서 제외합니다.

- 여러 저장소와 다른 프로그래밍 언어 지원
- 상속·reflection·monkey patch 등 복잡한 동적 호출의 완전한 분석
- 변경으로 인한 실제 오류 발생 여부 예측
- 자동 코드 수정과 Pull Request 생성
- 멀티 Agent와 Vector DB

## 평가

평가용 커밋마다 변경 함수, 영향 함수와 관련 테스트의 정답을 미리 작성하고 Agent 결과와 비교합니다.


테스트 실행 Trace는 어떤 테스트가 실제로 관련 함수를 실행했는지 확인하는 보조 근거로 사용합니다.

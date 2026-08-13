# Issue-to-PR AI Agent 최종 기획·아키텍처 명세

## 1. 개요

GitHub Issue를 감지하고 저장소 분석, 코드 작성·수정, 테스트, Draft PR 생성까지 수행하는
경량 AI 개발 Agent다. 기존 Code Impact Agent와 결합하지 않는 독립 프로젝트이며, 현재
대상 저장소는 `doyoon323/ax-test-repo`다.

LLM은 작업을 판단하고 구조화된 수정안을 작성한다. 로컬 Python 서버는 명령과 경로를
검증하고 실제 파일 수정, 테스트, Git 및 GitHub 작업을 수행한다.

## 2. Issue 감지 방식

현재 로컬 데모에서는 Poller 방식을 사용한다. 장기 실행·배포 환경에서는 즉시성, API 호출량,
명확한 delivery idempotency 때문에 서명 검증 Webhook을 우선한다.

- 15초마다 대상 저장소의 열린 Issue를 조회한다.
- `ai-fix` 라벨이 붙은 미처리 Issue만 접수한다.
- `OWNER`, `MEMBER`, `COLLABORATOR`가 작성한 Issue만 허용한다.
- Issue 제목과 Description을 모두 LLM에 전달한다.
- 저장소와 Issue 번호로 만든 식별자와 SQLite 작업 기록으로 중복 처리를 방지한다.
- `running` 작업은 재시작 시 복구하고, 429·5xx·네트워크 오류만 제한적으로 재시도한다.
- 최종 실패 뒤 Issue 제목·본문이 바뀌면 새 revision ID로 다시 접수한다.
- 게시 모드에서는 GitHub Issue의 단일 상태 댓글을 진행·재시도·완료·실패로 갱신한다.
- 단일 Worker가 Issue를 하나씩 순서대로 처리한다.

Webhook 엔드포인트도 구현돼 있지만 현재는 비활성화 상태다. Webhook 모드에서는
`issues.opened`, `issues.labeled` 이벤트와 HMAC 서명을 검증한다. 공개 URL이 필요 없는
로컬 데모에는 Poller가 적합하다.

## 3. 동작 흐름

1. Poller가 처리 가능한 Issue를 발견해 SQLite 작업 큐에 등록한다.
2. `origin/main`을 기준으로 Issue 전용 Git Worktree와 `agent/issue-...` 브랜치를 만든다.
3. LLM이 Issue와 저장소 정보를 보고 탐색 명령을 JSON으로 작성한다.
4. 서버가 명령 허용 목록과 경로를 검사한 뒤 실행한다.
5. 탐색 결과를 최대 1,000자 또는 50줄로 줄여 LLM에 관찰 데이터로 전달한다.
6. LLM이 `replace`, `create`, `append` 형식의 코드 수정안을 작성한다.
7. 서버가 수정 경로와 조건을 검증한 뒤 Worktree 안의 파일에 반영한다.
8. 모델 제안과 무관하게 서버가 `git diff --check`와 설정된 전체 회귀 테스트를 실행한다.
9. 실패하면 관찰 결과를 근거로 최대 1회 수정·재검증하고, 계속 실패하면 사람 검토로 전환한다.
10. 변경된 회귀 테스트가 수정 전 기준점에서 실패하고 최종 코드에서 성공하는지 확인한다.
11. 검증 성공 시 승인된 파일만 Stage, Commit, Push한다.
12. `main` 대상 Draft PR과 GitHub Check를 만들고 Issue 작성자를 담당자로 지정한다.
13. 원 Issue에 PR 링크를 남기고 임시 Worktree를 제거한다.

코드는 컴퓨터의 루트나 Agent 소스 폴더가 아니라 다음 임시 경로에서 작성된다.

```text
/private/tmp/issue-to-pr-agent/worktrees/issue-{번호}-{식별자}/
```

파일 위치는 기존 저장소 구조를 우선한다. 같은 책임은 같은 모듈에 두고, 책임이 다른 기능은
파일을 분리하며 테스트 경로는 소스 구조와 대응시키는 것을 목표 정책으로 한다.

## 4. Agent 제어 구조와 핵심 개념

완전 자율형 ReAct Loop가 아니라 다음 순서가 고정된 3단계 ReAct 유사 구조다.

```text
진단(diagnose) → 구현(patch) → 검증(verify)
```

각 단계는 `LLM 판단 → Tool 실행 → 결과 관찰` 방식으로 동작한다. 의미 단계는 세 개로
고정되며 검증 실패 시 1회의 `patch → verify` 교정만 허용한다. API 장애, JSON 형식 오류,
단계 또는 수정안 오류도 제한적으로 재시도한다.

- **LangGraph:** 사용하지 않는다. Python 제어기가 순서와 상태를 직접 관리한다.
- **RAG:** 사용하지 않는다. Vector DB나 Embedding 대신 현재 저장소를 CLI로 직접 검색한다.
- **LiteLLM:** Gemini와 Groq를 같은 호출 형식으로 연결하는 LLM 어댑터로 사용한다.
- **네이티브 Tool Calling:** 사용하지 않는다.
- **Tool 방식:** LLM이 Structured JSON으로 행동을 요청하고 서버가 해석하는 자체 프로토콜이다.

네이티브 Tool Calling을 제외한 주된 이유는 무료 한도 자체가 아니라, 현재 Tool과 단계가 적고
고정돼 있어 자체 JSON 방식이 단순하며 공급자 간 호환성과 실행 통제를 확보하기 쉽기 때문이다.
향후 테스트 실패 원인을 여러 번 탐색·수정하는 자율 Loop로 확장할 때 도입을 검토한다.

## 5. Tool 실행 방식

허용 Tool은 다음과 같다.

- 탐색: `rg`, `find`, `ls`
- 파일 조회: `sed`, `head`, `tail`
- Git 조회: `diff`, `status`, `show`, `grep`, `log`, `ls-files`
- 검증: `pytest`, `ruff`, `unittest`, `compileall`
- 파일 수정: `replace`, `create`, `append`

LLM은 Tool을 직접 실행하지 않는다. 서버는 `shell=True`를 사용하지 않고 argv 배열만
`subprocess`에 전달한다. `bash`, `sh`, 파이프, 리다이렉션, 임의 Python 실행과 네트워크
명령은 허용하지 않는다.

## 6. 모델 운영

- 기본 모델: `gemini/gemini-3.1-pro-preview` (품질 우선, 유료)
- 예비 모델: 기본 비활성화, 운영자가 명시한 경우에만 사용
- 공통 호출 계층: LiteLLM
- 단계 사이 최소 대기: 4.1초
- 기본 오류 재시도: 최대 2회
- Gemini 할당량 초과 또는 5xx 시 명시된 예비 모델로만 전환한다.
- Groq의 일시적 제한과 JSON 생성 오류는 제한적으로 재시도한다.
- 모델 이력과 공급자 응답의 입력·출력·전체 토큰 수를 작업 결과와 Draft PR에 기록한다.
- 작업별 토큰·예상 비용·실행 시간 상한을 넘으면 자동 중단한다.

## 7. 안전장치

- Issue 내용을 신뢰할 수 없는 데이터로 취급한다.
- 원본 checkout과 `main`을 직접 수정하지 않는다.
- `.env`, `.git`, 키·인증서와 GitHub Actions 경로 수정을 금지한다.
- Worktree 밖의 절대경로와 `../` 이동을 차단한다.
- 탐색 명령은 최대 30초, 격리 검증은 기본 120초로 분리한다.
- CLI 출력은 최대 1,000자 또는 50줄로 제한한다.
- 모델이 선택한 좁은 테스트만으로 게시할 수 없고, 서버 소유 필수 게이트가 실패하면 PR을 생성하지 않는다.
- Compose에서는 Agent와 무권한 Runner를 분리하고 테스트 Runner에는 토큰과 네트워크를 주지 않는다.
- 검증은 read-only root, capability 제거, CPU·메모리·PID 제한 컨테이너에서 실행한다.
- Worktree는 브랜치·코드 격리일 뿐이며 Docker 검증 격리를 대체하지 않는다.
- 게시 전에 GitHub 토큰 로그인과 기대 봇 로그인을 비교하고 커밋 작성자도 고정한다.
- 실제 변경이 없으면 `no-change`로 종료하고 PR을 생성하지 않는다.
- 파일 수·diff 줄 수를 넘는 복잡한 변경은 사람 검토 대상으로 중단한다.
- 자동 병합 없이 Draft PR만 생성해 사람이 최종 검토한다.

## 8. 현재 상태와 개선 방향

### 현재 지원

- Poller와 선택적 Webhook
- Issue별 Worktree·브랜치 격리
- Gemini 기본, 선택적 운영자 지정 Fallback
- 제한적 자동 재시도와 재시작 상태 복구
- GitHub 상태 댓글과 토큰·모델 사용 기록
- 비밀·네트워크가 없는 별도 Runner 컨테이너 기반 필수 검증 게이트
- fail-to-pass, 1회 자체 교정, 시간·복잡도·토큰·비용 제한
- 재시작 복구, Agent 브랜치 재게시, GitHub Check
- 구조화된 코드 수정과 제한된 검증
- Draft PR, 담당자 지정, Issue 댓글
- PR 본문의 `Closes #번호`

`Closes #번호`가 이미 포함되므로 PR이 기본 브랜치에 병합되면 GitHub가 Issue를 자동으로
닫는다. 별도의 Issue close API 구현은 필요하지 않다.

### 개선 과제

1. **실제 E2E:** GitHub Issue→Gemini→Draft PR 외부 통합은 별도 비용·변경 승인을 받아 검증한다.
2. **실행 이미지:** 저장소별 의존성을 잠금 파일로 설치하고 image digest를 고정한다.
3. **운영 상태:** SQLite 단일 Worker를 DB lease/heartbeat 기반 다중 Worker로 전환한다.
4. **누적 비용:** 시도별이 아니라 delivery 전체 사용량·비용을 실패 시에도 영속화한다.
5. **의미 정확성:** 사람이 작성한 acceptance test 또는 독립 검증 모델로 테스트 편향을 줄인다.
6. **강한 격리:** 공개 저장소 지원 전 gVisor, microVM 또는 일회성 Kubernetes Job을 사용한다.
7. **GitHub 정책:** Check를 Branch Protection의 Required Check로 강제하고 권한을 실검증한다.
8. **모델 수명:** Preview 종료·가격 변경을 감시하고 모델별 가격표를 운영 설정으로 분리한다.

---

# Architecture Specification: Issue-to-PR Lean AI Agent

## 1. Overview

GitHub Issue를 감지해 저장소를 분석하고 코드를 작성·수정한 뒤, 검증된 변경사항을 Draft PR로
제출하는 로컬 실행형 경량 AI Agent다.

## 2. Key Constraints & Tech Stack

- **Language:** Python 3.11~3.13
- **API Server:** FastAPI, Uvicorn
- **Issue Source:** Poller 기본, Webhook 선택
- **Architecture:** 고정 3단계 ReAct 유사 제어기
- **LLM Engine:** LiteLLM, 유료 Gemini 3.1 Pro Preview 기본, 선택적 Fallback
- **Tool Protocol:** JSON Schema 기반 자체 Tool 프로토콜
- **Execution:** Git Worktree와 네트워크·비밀 없는 별도 Runner 컨테이너
- **State:** SQLite 작업 큐, 중복 방지, 시도 횟수와 재시작 복구
- **Rate Limit:** 4.1초 간격, 기본 최대 2회 재시도
- **Max Steps:** 진단·구현·검증 3단계, 오류 교정 호출은 별도
- **Output Truncation:** 최대 1,000자 또는 50줄
- **Publishing:** 토큰 신원과 필수 검증 성공 시 Draft PR만 생성

## 3. Directory Structure

```text
issue-to-pr-agent/
├── src/issue_to_pr_agent/
│   ├── main.py
│   ├── config.py
│   ├── poller.py
│   ├── jobs.py
│   ├── worker.py
│   ├── service.py
│   ├── agent.py
│   ├── tools.py
│   ├── runner.py
│   ├── models.py
│   └── github_client.py
├── tests/
├── pyproject.toml
├── Dockerfile
├── Dockerfile.runner
├── compose.yaml
├── requirements.txt
├── README.md
└── FINAL_SPEC.md
```

## 4. Detailed Component Specifications

- **`main.py`:** FastAPI 앱, `/health`, `/webhook`, HMAC 검증과 생명주기 관리
- **`config.py`:** 모델, 저장소, Poller, 보안 및 실행 제한 환경변수 검증
- **`poller.py`:** 처리 가능한 열린 Issue 조회와 Worker 제출
- **`jobs.py`:** SQLite 작업 상태와 중복 처리 관리
- **`worker.py`:** 단일 비동기 작업 Queue와 재시작 복구
- **`service.py`:** Worktree 준비, Agent 실행, 게시와 정리 조정
- **`agent.py`:** 3단계 LLM 제어, Schema 검증, 재시도와 모델 Fallback
- **`tools.py`:** argv 기반 제한 명령 실행, 코드 수정, Timeout과 출력 제한
- **`runner.py`:** 파일 Queue 요청을 검증하고 네트워크 없는 컨테이너에서 테스트 실행
- **`models.py`:** Issue, 수정안, Tool 결과와 Agent 결과 데이터 계약
- **`github_client.py`:** Worktree·브랜치·Commit·Push 및 GitHub REST API 게시

## 5. Output Requirements

- 타입이 명시된 Python 코드와 검증 가능한 JSON 데이터 계약
- Issue 제목과 Description을 모두 분석
- LLM 응답, 명령, 수정 경로와 게시 파일 검증
- 민감 파일, 임의 셸과 Worktree 외부 접근 차단
- 테스트 실패 또는 변경 없음 상태에서 게시 중단
- Issue별 격리 작업과 중복 처리 방지
- Draft PR을 통한 사람의 최종 검토
- 구현과 문서에 대한 자동 테스트 및 Ruff 검사 유지

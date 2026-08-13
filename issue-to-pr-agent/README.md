# Issue-to-PR Agent

GitHub Issue를 받아 소규모 Python 저장소를 수정·검증하고 Draft PR을 만드는 독립 서비스입니다.
기존 `Code Impact Agent`와 코드·환경·실행 경로를 공유하지 않습니다.

```text
GitHub Issue(opened/labeled 또는 주기 조회)
  -> 저장소·작성자·ai-fix 라벨 검증
  -> SQLite 중복 방지·재시작 복구 + 단일 로컬 워커
  -> 읽기 전용 원본에서 별도 저장소 mirror + 임시 Git worktree 준비
  -> 파일 트리·정확 문자열·AST 선언 기반 Top-5 사전 지역화
  -> bounded loop: diagnose -> patch -> verify -> 실패 시 1회 교정
  -> 재현 테스트의 수정 전 유효 실패 + 수정 후 성공 + 전체 회귀 검증
  -> commit/push -> GitHub Check 성공 -> Draft PR -> assignee/comment
```

전체 결정사항과 비범위는 [FINAL_SPEC.md](FINAL_SPEC.md), 검증 수준과 테스트 분해는
[TEST_EVIDENCE.md](TEST_EVIDENCE.md)에 있습니다.

## 안전한 기본값

- `PUBLISH_ENABLED=false`: 처음에는 push와 GitHub API 변경을 하지 않습니다.
- Issue 본문은 신뢰하지 않는 입력으로 취급합니다.
- Agent는 `shell=True`, 임의 Python 실행, Git 쓰기 명령을 사용할 수 없습니다.
- `.env`, Git 내부 파일, 인증서·키 파일은 편집하거나 게시하지 않습니다.
- Compose에서는 원본 checkout을 읽기 전용으로 마운트하고, 별도 mirror/worktree만 수정합니다.
- Compose 운영에서는 Agent와 무권한 Runner를 분리합니다. Runner에는 토큰과 네트워크가 없습니다.
- Runner는 Worktree를 읽기 전용으로 마운트하고 임시 bytecode는 `/tmp`에만 기록합니다.
- 모델 제안과 별도로 서버가 `git diff --check`와 필수 회귀 테스트를 실행합니다.
- 게시 전 GitHub App ID·slug·설치 저장소·필수 권한을 검증합니다.
- 변경사항, fail-to-pass, 전체 회귀 테스트와 GitHub Check 기록이 모두 성공해야 Draft PR을 만듭니다.
- Agent 단계 10분·8파일·800줄·3만 토큰·예상 $0.50을 넘으면 사람 검토로 전환합니다.

이 안전장치는 컨테이너/VM 샌드박스를 대체하지 않습니다. 공개 저장소에서 불특정 사용자의
Issue를 처리하는 용도로는 아직 적합하지 않습니다.

## 설치

Python 3.11~3.13을 사용합니다. Agent 컨테이너의 런타임 의존성은 hash가 포함된
`requirements.lock`으로 고정하며, 격리 검증 이미지는 실행 전에 준비합니다.

```bash
cd issue-to-pr-agent
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
docker pull python:3.13-slim
```

현재 구성은 상위 프로젝트 `.env`의 기존 Gemini·Groq 설정을 먼저 읽고, 이 서비스의 `.env`에 있는
저장소·GitHub App 설정으로 덮어씁니다. 이 환경은 Agent에만 전달되며 Runner에는 전달되지 않습니다.
GitHub는 개인 계정 토큰 대신 설치 범위가 제한된 GitHub App의 1시간 설치 토큰을 자동 발급·갱신합니다.
PEM 키는 저장소 밖에 두고 Compose에서 Agent에만 읽기 전용으로 마운트합니다.

```dotenv
GEMINI_API_KEY=...
GROQ_API_KEY=...
GITHUB_AUTH_MODE=app
GITHUB_APP_ID=4583096
GITHUB_APP_INSTALLATION_ID=153482646
GITHUB_APP_SLUG=auto-coding-issues
GITHUB_APP_PRIVATE_KEY_PATH=/Users/dyn/.config/issue-to-pr-agent/github-app.pem
GITHUB_TOKEN= # App 모드에서는 반드시 비움
GITHUB_WEBHOOK_SECRET=충분히_긴_임의의_문자열 # webhook 모드에서만 필요
GITHUB_REPOSITORY=owner/repository
WORKSPACE_PATH=/absolute/path/to/local/repository
ISSUE_SOURCE=poll
LLM_MODEL=gemini/gemini-3.1-pro-preview
REQUIRED_VERIFICATION_COMMANDS=[["python","-m","unittest","discover","-s","tests","-v"]]
```

로컬 모델로 바꾸려면 Gemini 키 없이 다음처럼 설정할 수 있습니다.

```dotenv
LLM_MODEL=ollama/qwen2.5-coder
LLM_API_BASE=http://localhost:11434
```

GitHub App은 대상 저장소에만 설치하고 다음 Repository permissions를 설정합니다.

- Contents: Read and write
- Pull requests: Read and write
- Issues: Read and write
- Checks: Read and write (`GITHUB_CHECKS_ENABLED=true`일 때)
- Metadata: Read

App 모드의 Git fetch/push는 HTTPS와 단기 설치 토큰을 사용하며 토큰을 remote URL에 저장하지 않습니다.
기본값은 정확히 `github.com/<GITHUB_REPOSITORY>`인 HTTPS origin만 허용합니다.
`WORKSPACE_PATH`는 운영자가 신뢰하는 로컬 clone이어야 하며 임의 사용자가 등록할 수 없습니다.

## 실행: Docker Compose

`.env`의 `WORKSPACE_PATH`, `GITHUB_APP_PRIVATE_KEY_PATH`, `ISSUE_TO_PR_RUNTIME_PATH`를 호스트
절대경로로 둔 뒤 실행합니다. 마지막 경로에는 mirror, Worktree, Runner queue, SQLite 상태가
저장되며 저장소와 분리된 전용 디렉터리를 사용합니다.

```bash
docker compose up --build
```

컨테이너도 기본은 `PUBLISH_ENABLED=false`입니다. dry-run을 확인한 뒤에만
`CONTAINER_PUBLISH_ENABLED=true docker compose up --build`로 게시를 엽니다. Agent는
GitHub·Gemini에 접근하지만 테스트는 별도 Runner에서 실행됩니다. Runner는 네트워크 차단,
capability 제거, read-only root, CPU·메모리·PID 제한을 사용하며 API 토큰을 받지 않습니다.
추가 의존성이 필요한 저장소는 `Dockerfile.runner`를 잠금 파일 기준으로 확장해야 합니다.

## 실행: 로컬 Poll 모드

```bash
.venv/bin/python -m uvicorn --app-dir src issue_to_pr_agent.main:create_app --factory --port 8000
```

`ISSUE_SOURCE=poll`은 로컬 데모용입니다. 별도 공개 URL 없이 15초마다 열린 Issue를 조회합니다. `ai-fix` 라벨이
있고 작성자 관계가 `OWNER`, `MEMBER`, `COLLABORATOR`인 Issue만 한 번씩 큐에 넣습니다.
Patch 단계에서 실제 파일 변경이 없으면 성공으로 보고하지 않고 사람 검토가 필요한 실패로 종료합니다.
따라서 모델의 설명만으로 `no-change` 완료나 Draft PR 생략을 정당화할 수 없습니다.
기본 모델은 코드 수정 품질을 우선한 유료 Gemini 3.1 Pro Preview입니다. 현재 기본 비용 계산은
2026-08-13 기준 [공식 Standard 단가](https://ai.google.dev/gemini-api/docs/pricing)의 입력 $2/백만,
출력 $12/백만(요청 20만 토큰 이하)을 사용합니다. 사용 모델과 공급자가
보고한 토큰 수는 SQLite 결과와 Draft PR에 남습니다. `LLM_FALLBACK_MODEL`을 설정한 경우에만
429 또는 5xx 뒤 예비 모델로 전환하며, 인증·요청 형식 오류에는 전환하지 않습니다.

429·5xx·네트워크 오류와 OS 프로세스 hard timeout만 최대 3회 시도합니다. 비용과 토큰은
delivery 전체 시도에 걸쳐 SQLite에 누적하므로 재시도로 상한을 우회할 수 없습니다. 테스트 실패는
관찰 결과로 1회만 수정·재검증합니다. 서비스 종료 중인 작업은 `queued`로 복구하며, 정지한
프로세스 그룹은 강제 종료합니다. 최종 실패 뒤 제목·본문을 보완하면 새 revision으로 재접수됩니다.
게시 모드에서는 Issue 상태 댓글과 검증 Check를 남깁니다.

## 선택: Webhook 모드

배포 환경에서는 Webhook을 우선합니다. `ISSUE_SOURCE=webhook`과 `GITHUB_WEBHOOK_SECRET`을 설정한 뒤 터널을
엽니다.

```bash
ngrok http 8000
```

GitHub 저장소의 `Settings -> Webhooks -> Add webhook`에서 설정합니다.

- Payload URL: `https://<ngrok 주소>/webhook`
- Content type: `application/json`
- Secret: `.env`의 `GITHUB_WEBHOOK_SECRET`와 같은 값
- Events: Issues

처리할 Issue에 `ai-fix` 라벨을 붙입니다. 처음에는 dry-run으로 코드 작성과 테스트까지만
동작합니다. 로컬 검증을 마친 뒤에만 `.env`의 `PUBLISH_ENABLED=true`로 변경하고 서버를
재시작하세요.

## 검증

```bash
.venv/bin/pytest -q
.venv/bin/coverage run -m pytest -q
.venv/bin/coverage report
.venv/bin/ruff check
.venv/bin/ruff format --check
```

`/health`는 서버 상태와 게시 활성화 여부만 반환합니다. 작업 실패 원인은 로컬 로그와
`.state/jobs.sqlite3`에 기록됩니다. `.state`와 `.env`는 Git에서 제외됩니다.

## 발표 시 명확히 밝힐 남은 위험

1. 강화된 현재 코드의 GitHub Issue→유료 Gemini→Draft PR 외부 E2E는 아직 실행하지 않았습니다.
2. 정확 문자열·AST 기반 지역화의 Recall@5와 실제 Issue 해결률은 표본 acceptance issue로 측정해야 합니다.
3. LLM이 만든 재현 테스트가 사용자의 의미 요구사항과 일치하는지는 자동으로 완전히 보장하지 못합니다.
4. Runner는 대상 저장소 의존성을 자동 설치하지 않아 저장소별 잠금 이미지와 digest가 필요합니다.
5. Agent 컨테이너는 네트워크·App 개인키·단기 토큰·쓰기 가능한 mirror를 함께 가지므로 침해 시 단일 실패점입니다.
6. SQLite 단일 Worker라 다중 인스턴스, 분산 lease와 수평 확장은 지원하지 않습니다.
7. 일반 Docker 격리이므로 공개 저장소에는 gVisor·VM 같은 더 강한 샌드박스가 필요합니다.
8. Check의 Required 정책과 Preview 모델의 수명·가격은 GitHub/Google 외부 설정에 의존합니다.

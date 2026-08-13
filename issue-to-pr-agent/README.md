# Issue-to-PR Agent

GitHub Issue를 받아 소규모 Python 저장소를 수정·검증하고 Draft PR을 만드는 독립 서비스입니다.
기존 `Code Impact Agent`와 코드·환경·실행 경로를 공유하지 않습니다.

```text
GitHub Issue(opened/labeled 또는 주기 조회)
  -> 저장소·작성자·ai-fix 라벨 검증
  -> SQLite 중복 방지·재시작 복구 + 단일 로컬 워커
  -> 임시 Git worktree
  -> bounded loop: diagnose -> patch -> verify
  -> 서버 소유 필수 회귀 테스트 + 격리 검증 성공
  -> commit/push -> Draft PR -> assignee/comment
```

전체 결정사항과 비범위는 [FINAL_SPEC.md](FINAL_SPEC.md)에 있습니다.

## 안전한 기본값

- `PUBLISH_ENABLED=false`: 처음에는 push와 GitHub API 변경을 하지 않습니다.
- Issue 본문은 신뢰하지 않는 입력으로 취급합니다.
- Agent는 `shell=True`, 임의 Python 실행, Git 쓰기 명령을 사용할 수 없습니다.
- `.env`, Git 내부 파일, 인증서·키 파일은 편집하거나 게시하지 않습니다.
- 원본 checkout 대신 `origin/main`에서 만든 임시 worktree만 수정합니다.
- 검증 명령은 기본적으로 네트워크·권한·자원을 제한한 Docker 컨테이너에서 실행합니다.
- 모델 제안과 별도로 서버가 `git diff --check`와 필수 회귀 테스트를 실행합니다.
- 게시 전 토큰의 GitHub 로그인과 `GITHUB_EXPECTED_LOGIN`이 일치해야 합니다.
- 변경사항과 필수 검증 성공이 모두 있어야 Draft PR을 만들 수 있습니다.

이 안전장치는 컨테이너/VM 샌드박스를 대체하지 않습니다. 공개 저장소에서 불특정 사용자의
Issue를 처리하는 용도로는 아직 적합하지 않습니다.

## 설치

Python 3.11~3.13을 사용합니다. 격리 검증 이미지는 실행 전에 준비합니다.

```bash
cd issue-to-pr-agent
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
docker pull python:3.13-slim
```

현재 로컬 구성은 상위 프로젝트 `.env`의 기존 Gemini·Groq 키를 **프로그램 실행 시에만** 읽고,
이 서비스의 `.env`에는 저장소 연결 정보만 둡니다. GitHub 토큰을 비워두면 `gh auth token`을
실행 중에 읽으므로 토큰을 파일이나 로그에 복사하지 않아도 됩니다.

```dotenv
GEMINI_API_KEY=...
GROQ_API_KEY=...
GITHUB_TOKEN=... # 선택: 비우면 gh 로그인 사용
GITHUB_WEBHOOK_SECRET=충분히_긴_임의의_문자열 # webhook 모드에서만 필요
GITHUB_REPOSITORY=owner/repository
WORKSPACE_PATH=/absolute/path/to/local/repository
ISSUE_SOURCE=poll
LLM_MODEL=gemini/gemini-3.1-pro-preview
GITHUB_EXPECTED_LOGIN=issue-agent-bot
REQUIRED_VERIFICATION_COMMANDS=[["python","-m","unittest","discover","-s","tests","-v"]]
```

로컬 모델로 바꾸려면 Gemini 키 없이 다음처럼 설정할 수 있습니다.

```dotenv
LLM_MODEL=ollama/qwen2.5-coder
LLM_API_BASE=http://localhost:11434
```

Fine-grained GitHub 토큰에는 대상 저장소의 다음 권한이 필요합니다.

- Contents: Read and write
- Pull requests: Read and write
- Issues: Read and write
- Metadata: Read

로컬 `origin`에도 해당 브랜치를 push할 인증이 설정되어 있어야 합니다.

## 실행: 로컬 Poll 모드

```bash
.venv/bin/python -m uvicorn --app-dir src issue_to_pr_agent.main:create_app --factory --port 8000
```

`ISSUE_SOURCE=poll`은 로컬 데모용입니다. 별도 공개 URL 없이 15초마다 열린 Issue를 조회합니다. `ai-fix` 라벨이
있고 작성자 관계가 `OWNER`, `MEMBER`, `COLLABORATOR`인 Issue만 한 번씩 큐에 넣습니다.
이미 코드가 요구사항을 충족하면 PR을 억지로 만들지 않고 `no-change` 댓글을 남깁니다.
기본 모델은 코드 수정 품질을 우선한 유료 Gemini 3.1 Pro Preview입니다. 사용 모델과 공급자가
보고한 토큰 수는 SQLite 결과와 Draft PR에 남습니다. `LLM_FALLBACK_MODEL`을 설정한 경우에만
429 또는 5xx 뒤 예비 모델로 전환하며, 인증·요청 형식 오류에는 전환하지 않습니다.

작업 실패는 429·5xx·네트워크 오류에 한해 최대 2회 재시도합니다. 중단된 `running` 작업도
SQLite 시도 횟수로 복구합니다. 최종 실패 뒤 제목·본문을 보완하면 새 revision으로 재접수됩니다.
게시 모드에서는 하나의 GitHub 상태 댓글을 갱신합니다.

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
.venv/bin/ruff check
.venv/bin/ruff format --check
```

`/health`는 서버 상태와 게시 활성화 여부만 반환합니다. 작업 실패 원인은 로컬 로그와
`.state/jobs.sqlite3`에 기록됩니다. `.state`와 `.env`는 Git에서 제외됩니다.

## 의도적으로 남긴 범위

- `compileall` 성공만으로 수정이 맞다고 판단하지 않습니다. 기본 필수 게이트는 전체 unittest입니다.
- 수정 전 실패와 수정 후 성공을 자동 비교하는 fail-to-pass 증명은 아직 강제하지 않습니다.
- Docker는 검증 프로세스를 격리하지만 의존성을 자동 설치하지 않습니다. 저장소별 사전 빌드
  이미지를 `VERIFICATION_CONTAINER_IMAGE`로 지정해야 합니다.
- Gemini 3.1 Pro는 Preview이므로 모델 종료 공지를 확인하고 교체해야 합니다.

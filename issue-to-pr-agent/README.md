# Issue-to-PR Agent

GitHub Issue를 받아 소규모 Python 저장소를 수정·검증하고 Draft PR을 만드는 독립 서비스입니다.
기존 `Code Impact Agent`와 코드·환경·실행 경로를 공유하지 않습니다.

```text
GitHub Issue(opened/labeled 또는 주기 조회)
  -> 저장소·작성자·ai-fix 라벨 검증
  -> SQLite 중복 방지 + 단일 로컬 워커
  -> 임시 Git worktree
  -> bounded loop: diagnose -> patch -> verify
  -> 검증 성공
  -> commit/push -> Draft PR -> assignee/comment
```

전체 결정사항과 비범위는 [FINAL_SPEC.md](FINAL_SPEC.md)에 있습니다.

## 안전한 기본값

- `PUBLISH_ENABLED=false`: 처음에는 push와 GitHub API 변경을 하지 않습니다.
- Issue 본문은 신뢰하지 않는 입력으로 취급합니다.
- Agent는 `shell=True`, 임의 Python 실행, Git 쓰기 명령을 사용할 수 없습니다.
- `.env`, Git 내부 파일, 인증서·키 파일은 편집하거나 게시하지 않습니다.
- 원본 checkout 대신 `origin/main`에서 만든 임시 worktree만 수정합니다.
- 변경사항과 검증 성공이 모두 있어야 Draft PR을 만들 수 있습니다.

이 안전장치는 컨테이너/VM 샌드박스를 대체하지 않습니다. 공개 저장소에서 불특정 사용자의
Issue를 처리하는 용도로는 아직 적합하지 않습니다.

## 설치

Python 3.11~3.13을 사용합니다.

```bash
cd issue-to-pr-agent
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
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
LLM_FALLBACK_MODEL=groq/openai/gpt-oss-120b
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

`ISSUE_SOURCE=poll`이면 별도 공개 URL 없이 15초마다 열린 Issue를 조회합니다. `ai-fix` 라벨이
있고 작성자 관계가 `OWNER`, `MEMBER`, `COLLABORATOR`인 Issue만 한 번씩 큐에 넣습니다.
이미 코드가 요구사항을 충족하면 PR을 억지로 만들지 않고 `no-change` 댓글을 남깁니다.
Gemini가 429 할당량 초과이거나 5xx 재시도 후에도 실패하면 같은 Issue의 남은 단계를 Groq
`openai/gpt-oss-120b`로 전환합니다. 인증·요청 형식 오류에는 전환하지 않습니다.

## 선택: Webhook 모드

즉시 감지가 필요하면 `ISSUE_SOURCE=webhook`과 `GITHUB_WEBHOOK_SECRET`을 설정한 뒤 터널을
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

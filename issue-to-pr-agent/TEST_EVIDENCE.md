# Issue-to-PR Agent 검증 증거

2026-08-14 로컬·GitHub App 검증 결과다. 테스트가 증명하는 범위와 외부 환경에서 아직 검증하지 않은 범위를
구분하며, 숫자만으로 실제 Issue 해결 정확도를 주장하지 않는다.

## 실행 결과

```text
pytest: 55 passed
branch coverage: 74% (2,234 statements, 726 branches)
Ruff check/format: passed
Docker Compose config: passed
Agent/Runner images: built successfully with hashed Agent dependencies
GitHub App: auto-coding-issues[bot] identity, target repository, required permissions passed
Agent container: read-only PEM mount and GitHub App authentication passed
Git HTTPS: short-lived installation token + GIT_ASKPASS ls-remote passed
Runner runtime: healthy, network=none, rootfs=read-only, cap-drop=ALL
```

재현 명령:

```bash
.venv/bin/coverage run -m pytest -q
.venv/bin/coverage report
.venv/bin/ruff check .
.venv/bin/ruff format --check .
docker compose config --quiet
docker compose build
```

## 테스트 55개 분해

| 영역 | 개수 | 보장하는 내용 |
|---|---:|---|
| Agent 제어기 | 12 | 3단계 순서, Schema, fallback, 1회 교정, delivery 누적 비용, fail-to-pass |
| 사전 지역화 | 4 | 정확 일치 순위, Top-5/문맥 제한, AST 선언, 민감 경로·symlink 차단 |
| Tool·Runner 정책 | 9 | argv/path 허용 목록, 격리 실행, 복잡도 제한, ImportError 재현 거부 |
| Git·게시 조정 | 4 | mirror/worktree, 승인 파일 push, dry-run, Check 실패 시 PR 차단 |
| Webhook·Poller·작업 복구 | 21 | HMAC, 권한, 중복, 누적 원장, 재시도, hard kill, 종료 복구, 상태 표시 |
| GitHub App 인증 | 5 | JWT·설치 토큰 cache, 신원·저장소·권한 거부, PAT 혼용 차단, Git 인증 환경 |

## Branch coverage

| 모듈 | 비율 | 해석 |
|---|---:|---|
| localization | 92% | 파일 선별, AST 선언, 경계·fallback 중심 |
| jobs | 85% | SQLite 상태 전이, 복구와 누적 사용량 |
| worker | 82% | 일시/결정적 실패, hard timeout과 종료 복구 |
| agent | 80% | 3단계 실행, correction, budget, fail-to-pass |
| config | 81% | 주요 운영 제한값 검증 |
| GitHub App auth | 80% | JWT·설치 토큰, 필수 권한과 저장소 접근 검증 |
| service | 74% | dry-run, Check 선행과 게시 조정 |
| tools | 73% | 명령·경로·편집·격리 backend 정책 |
| main | 70% | Webhook 접수·중복 처리 중심 |
| GitHub client | 60% | 로컬 Git과 REST 요청 생성; 게시 API 전체 E2E는 미검증 |
| poller | 55% | 단일 scan; 장기 polling loop 장애는 미검증 |
| runner | 50% | 핵심 허용/거부; 모든 CLI 오류 분기는 미검증 |
| **전체** | **74%** | branch coverage, 최소 기준 70% |

## 장애·복구 및 검증 게이트

| 시나리오 | 증거 수준 | 결과 |
|---|---|---|
| 기준 코드 ImportError | 단위 테스트 | 버그 재현으로 인정하지 않고 게시 차단 |
| 일시적 503 | 프로세스 통합 테스트 | 두 번째 시도에서 완료 |
| 결정적 patch 오류 | 프로세스 통합 테스트 | 재시도 없이 실패 |
| 정지한 작업 | hard timeout 장애 주입 | 프로세스 그룹 kill 후 실패 상태 기록 |
| 서비스 종료 중 작업 | 종료 장애 주입 | 프로세스 kill 후 `queued` 복구 |
| 누적 비용 | SQLite/Agent 단위 테스트 | 재시도 전 사용량을 다음 시도 예산에 포함 |
| GitHub Check 기록 실패 | Service 단위 테스트 | Draft PR 미생성 |
| 같은 Agent 브랜치 재게시 | 로컬 bare Git 통합 | 기존 원격 SHA를 lease로 갱신 |

## 아직 증명하지 않은 핵심

- 강화된 현재 코드의 GitHub Issue → 유료 Gemini → push → Draft PR 전체 E2E
- 지역화 후보의 Recall@5와 실제 Issue 해결률
- 실제 Check·Draft PR 게시, assignee와 Branch Protection 상호작용
- 공급자 usage 응답과 실제 청구 비용의 일치
- 저장소별 의존성이 포함된 Runner 이미지와 image digest 고정
- LLM이 만든 테스트가 사용자의 의미 요구사항과 일치한다는 독립 검증

따라서 현재 표현은 **"제어·지역화·격리·로컬 복구는 검증했지만 외부 E2E와 해결 정확도는
아직 별도 acceptance 검증이 필요하다"**가 정확하다.

참고로 2026-08-13 `ax-test-repo`의 `main` Branch Protection API를 읽기 전용으로 조회했으나,
비공개 저장소의 GitHub 플랜 제한으로 HTTP 403이 반환되어 설정 유무를 확정하지 못했다.

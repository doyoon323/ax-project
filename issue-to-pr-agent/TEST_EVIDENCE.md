# Issue-to-PR Agent 검증 증거

2026-08-13 로컬 검증 결과다. 테스트가 증명하는 범위와 외부 환경에서 아직 검증하지 않은 범위를
구분한다. 숫자만으로 전체 시스템의 정확성을 주장하지 않는다.

## 실행 결과

```text
pytest: 40 passed
branch coverage: 72% (1,734 statements, 574 branches)
Ruff check/format: passed
Docker Compose config: passed
Agent/Runner images: built successfully
Runner smoke: network disabled + read-only worktree에서 unittest/compileall passed
```

재현 명령:

```bash
.venv/bin/coverage run -m pytest -q
.venv/bin/coverage report
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
docker compose config --quiet
docker compose build
```

## 테스트 40개 분해

| 영역 | 개수 | 보장하는 내용 |
|---|---:|---|
| Agent 제어기 | 11 | 3단계 순서, JSON Schema, 모델 fallback, 1회 교정, 토큰 중단, fail-to-pass 거부 |
| Tool·Runner 정책 | 8 | argv/path 허용 목록, 민감 경로 차단, 복잡도 제한, 기준 코드 실패, Runner 요청 거부 |
| Git·게시 조정 | 3 | 승인 파일만 push, 기존 원격 Agent 브랜치 lease 갱신, dry-run/게시 순서 |
| Webhook·Poller·작업 복구 | 18 | HMAC, 권한 필터, 중복 방지, 선택적 재시도, 프로세스 kill 복구, 상태 댓글·Check 요청 |

## Branch coverage

| 모듈 | 비율 | 해석 |
|---|---:|---|
| jobs | 90% | SQLite 상태 전이와 복구 중심 |
| worker | 81% | 일시/결정적 실패와 재시도 중심 |
| config | 80% | 주요 운영 제한값 검증 |
| agent | 79% | 3단계 실행, correction, budget, fail-to-pass |
| tools | 72% | 명령·경로·편집·격리 backend 정책 |
| service | 71% | dry-run과 성공 게시 orchestration |
| main | 70% | Webhook 접수·중복 처리 중심 |
| GitHub client | 61% | 로컬 Git 통합과 REST 요청 생성; 실제 GitHub 응답은 미검증 |
| poller | 55% | 단일 scan; 장기 polling loop 장애는 미검증 |
| runner | 50% | 핵심 허용/거부와 Docker smoke; 모든 CLI 오류 분기는 미검증 |
| **전체** | **72%** | branch coverage, 최소 기준 70% |

## 장애·복구 증거

| 시나리오 | 증거 수준 | 결과 |
|---|---|---|
| 일시적 503 후 재시도 | 단위 테스트 | 두 번째 시도에서 완료 |
| 결정적 patch 오류 | 단위 테스트 | 재시도 없이 실패 |
| `running` 프로세스 강제 종료 | 로컬 장애 주입 | 재시작 시 `queued` 복구 |
| 재시도 한도 소진 후 재시작 | SQLite 통합 테스트 | `failed` 유지 |
| 같은 Agent 브랜치 재게시 | 로컬 bare Git 통합 | 기존 원격 SHA를 lease로 갱신 |
| push 후 실제 GitHub API 5xx | 미검증 | 로직과 로컬 Git 증거만 존재 |

## 아직 증명하지 않은 핵심

- 실제 GitHub Issue → 유료 Gemini → 테스트 → push → Draft PR 전체 E2E
- 실제 토큰의 Checks 권한과 Branch Protection 상호작용
- 저장소별 의존성이 설치된 Runner 이미지
- LLM이 생성한 테스트가 사용자의 의미 요구사항과 일치한다는 독립 검증
- 재시도를 포함한 일/저장소 단위 누적 비용 상한

따라서 현재 표현은 **"핵심 제어·격리·로컬 복구는 검증했지만 외부 E2E는 미검증"**이 정확하다.

참고로 2026-08-13 `ax-test-repo`의 `main` Branch Protection API를 읽기 전용으로
조회했으나, 비공개 저장소의 GitHub 플랜 제한으로 HTTP 403이 반환되어 설정 유무를
확정하지 못했다.

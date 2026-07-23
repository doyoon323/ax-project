# Project Overview

## 서비스 장애 원인 가설 검증 AI Agent

> Codedang의 `client-api`와 직접 연관된 Kubernetes 워크로드에서 장애 증거를 수집하고, 원인 가설을 검증하는 읽기 전용 RCA(Root Cause Analysis) Agent


## 1. 문제와 목적

서비스에서 5xx, 응답 지연, Pod 재시작 또는 채점 지연이 발생해도 원인이 무엇인지 판단하기 어렵다.

- 새 버전의 설정이나 코드가 잘못되었을 수 있다.
- Pod가 OOM으로 종료되거나 Probe에 실패했을 수 있다.
- 다른 서비스 연결에 문제가 생겼을 수 있다.
- 관련된 서비스에 병목이 발생했을 수 있다.
- 정상 배포 직후 별개의 인프라 문제가 발생했을 수도 있다.

운영자는 원인을 찾기 위해 Loki, Prometheus, Kubernetes, Argo CD와 Git을 오가며 장애 시점의 데이터를 직접 맞춰야 한다.

이 프로젝트의 목적은 다음과 같다.

> **여러 도구와 워크로드에 흩어진 장애 증거를 자동으로 수집·정렬하고, 원인 후보를 지지하거나 기각하여 운영자의 초기 장애 분석 시간을 줄인다.**

이 프로젝트는 로그 수집 플랫폼이나 자동 복구 시스템을 만들지 않는다. 기존 관측 시스템의 데이터를 조회하여 다음 질문에 답하는 조사 Agent를 만든다.

1. 현재 증상을 설명할 수 있는 원인 후보는 무엇인가?
2. 후보를 지지하거나 반박하는 증거는 무엇인가?
3. 어떤 후보를 기각할 수 있는가?
4. 다음으로 무엇을 확인하거나 조치해야 하는가?



## 2. 확인할 증거

모든 로그를 LLM에 전달하지 않는다. 장애 시점과 가설에 필요한 데이터만 조회하고 구조화한다.

| 출처 | 확인 내용 |
|---|---|
| Loki | 예외, 5xx, timeout, DB·Redis·RabbitMQ 연결 오류, 이전 컨테이너 로그 |
| Prometheus | 요청량, 에러율, p95 지연시간, CPU·메모리, throttling, 재시작, RabbitMQ 큐 상태 |
| Kubernetes API | Pod 상태, 종료 사유, OOMKilled, Probe 실패, Event, Deployment·ReplicaSet |
| Argo CD·Registry | 배포 시각, revision, 이미지 digest |
| Git | 배포 이미지와 연결된 commit 및 diff |

각 조회 결과에는 시간, 출처, 대상 리소스와 원본을 찾을 수 있는 증거 ID를 부여한다. Secret 값과 개인정보·토큰은 수집하지 않거나 LLM 전달 전에 마스킹한다.

## 3. Agent 동작

사용자가 장애 시각과 증상을 입력하면 Agent가 분석을 시작한다.

```text
장애 정보 입력
  → 서비스의 초기 ±15분 데이터 조회
  → 증거 타임라인 구성
  → 원인 가설 최대 3개 생성
  → 가설에 따라 관련 워크로드 추가 조회
  → 지지 근거와 반증 비교
  → 후보 기각·채택·순위화
  → RCA 보고서 생성
```

증거가 부족하면 조회 시간 범위를 확장한다. 시간 계산, 수치 비교, 파싱과 정렬은 일반 코드가 담당하고, LLM은 가설 생성, 다음 도구 선택, Git diff 해석과 보고서 작성을 담당한다.

판정은 네 단계로 제한한다.

- `후보`: 추가 검토가 필요한 가설
- `유력`: 여러 증거가 지지하고 주요 반증이 없는 상태
- `확정`: 기존 사후 분석, 격리 환경 재현 또는 롤백 결과로 확인된 상태
- `판단 불가`: 증거가 부족하거나 충돌하는 상태

읽기 전용 분석에서는 원칙적으로 `유력`까지만 제시한다. 수치형 신뢰도는 별도로 보정할 수 없으므로 사용하지 않는다.

## 6. 6주 MVP

| 구분 | 범위 |
|---|---|
| 실행 방식 | 수동 CLI 입력 |
| 분석 진입점 | `client-api` |
| 관련 워크로드 | Redis, RabbitMQ, Iris allowlist |
| 데이터 | Loki, Prometheus, Kubernetes, Argo CD, 이미지 digest, Git |
| 분석 범위 | 초기 ±15분, 증거 부족 시 확장 |
| 원인 후보 | 최대 3개 |
| 출력 | 증거 ID가 포함된 Markdown·JSON 보고서 |
| 권한 | 운영 환경을 변경할 수 없는 read-only |

Alertmanager 자동 연동, Tempo Trace, 웹 UI와 자동 복구는 MVP에서 제외한다. k6는 성능 분석 기능이 아니라 격리 환경에서 트래픽을 만드는 평가 도구로만 사용한다.

## 참고 자료

###  1. 서비스 

[Codedang](https://github.com/skkuding/codedang)은 SKKUding 팀이 개발·운영하는 오픈소스 Online Judge 시스템이다. 사용자는 웹에서 문제를 풀고 코드를 제출하여 채점 결과를 확인할 수 있다.

이 프로젝트의 분석 진입점인 [`client-api`](https://github.com/skkuding/codedang/tree/main/apps/backend/apps/client)는 문제·대회·사용자·제출 요청을 처리하는 사용자용 백엔드 API다. Kubernetes에서는 여러 Pod로 실행되며 PostgreSQL, Redis, RabbitMQ와 통신한다. 코드 제출은 RabbitMQ를 거쳐 채점 워커인 Iris에서 처리된다.

```mermaid
flowchart LR
    U["사용자"] --> C["client-api<br/>문제·대회·제출 API"]
    C --> DB["PostgreSQL"]
    C --> R["Redis"]
    C --> Q["RabbitMQ"]
    Q --> I["Iris<br/>채점 워커"]
    I --> DB
```

위 그림은 이 프로젝트와 관련된 흐름만 단순화한 것이다. Codedang 전체 구조를 분석 대상으로 삼지는 않는다.

관련 코드와 인프라는 다음 저장소에서 확인할 수 있다.

- [Codedang 저장소](https://github.com/skkuding/codedang)
- [client-api 소스 코드](https://github.com/skkuding/codedang/tree/main/apps/backend/apps/client)
- [client-api Kubernetes 구성](https://github.com/skkuding/codedang/tree/main/infra/k8s/client-api)
- [RabbitMQ 구성](https://github.com/skkuding/codedang/tree/main/infra/k8s/rabbitmq)
- [Iris 채점 워커](https://github.com/skkuding/codedang/tree/main/apps/iris)

### 2. 분석 대상


- `client-api`의 모든 Pod와 이전 컨테이너
- Pod·Deployment·ReplicaSet·Kubernetes Event
- 장애 전후 로그와 핵심 메트릭
- `client-api`의 배포 revision, 이미지 digest, Git commit·diff

### 증상에 따라 추가 분석

| 증상 | 추가 워크로드 |
|---|---|
| Redis 연결·캐시 오류 | Redis |
| 메시지 발행·큐 지연 | RabbitMQ |
| 제출·채점 지연 | RabbitMQ와 Iris |

 전체 클러스터를 조회하지 않고 `client-api`에서 직접 이어지는 1-hop 범위까지만 분석한다.



### 3. 과거 장애 사례

- 5월 대회 중 발생한 장애
- `release` 배포 과정에서 발생한 장애





### 4. KubeRCA

[KubeRCA](https://github.com/kube-rca/kuberca)의 Kubernetes context 조회와 읽기 전용 분석 구조를 참고한다. 본 프로젝트는 Codedang의 `client-api`와 직접 연관된 워크로드, Loki·Argo CD·Git 증거 연결과 가설 검증에 범위를 맞춰 독립적으로 구현한다.

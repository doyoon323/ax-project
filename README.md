# Kubernetes 마이크로서비스 장애 원인 가설 검증 AI Agent

> Evidence-driven, read-only RCA Agent for Kubernetes microservices

Kubernetes 마이크로서비스 장애가 발생했을 때 로그, 메트릭, Kubernetes 상태, 배포 이력, Git 변경사항을 한 타임라인으로 연결하고 **원인 가설을 검증하여 근거와 함께 제시하는 읽기 전용 RCA(Root Cause Analysis) Agent**입니다.

## 문제

MSA에서는 장애 증상과 실제 원인이 서로 다른 서비스에서 나타날 수 있습니다. 운영자는 여러 서비스의 로그를 시간순으로 맞추고, Loki·Prometheus·Kubernetes·Argo CD·Git을 오가며 원인을 좁혀야 합니다.

이 프로젝트는 다음 작업을 자동화합니다.

- 장애 시점과 관련된 서비스·인프라 증거 수집
- 로그·메트릭·배포·커밋의 시간대 정렬
- 원인 후보 생성과 추가 조회
- 지지 근거와 반증을 비교한 후보 채택·기각
- 근거가 포함된 RCA 보고서 생성

## 6주 MVP

| 구분 | 범위 |
|---|---|
| 분석 진입점 | `client-api`와 직접 의존 구성요소 |
| 입력 | 장애 서비스, 발생 시각, 증상 |
| 데이터 | Loki, Prometheus, Kubernetes, Argo CD, 이미지 digest, Git commit·diff |
| 분석 범위 | 초기 ±15분, 필요 시 확장 |
| 권한 | 운영 환경 변경이 불가능한 read-only |
| 출력 | Markdown·JSON RCA 보고서 |

```text
장애 정보 입력
  → 관련 증거 수집 및 타임라인 정렬
  → 원인 가설 생성
  → 가설별 추가 데이터 조회
  → 후보 채택·기각·순위화
  → 근거·반증·대응 방안 보고서
```

판정은 `후보`, `유력`, `확정`, `판단 불가`로 제한합니다. 읽기 전용 분석에서는 원칙적으로 `유력`까지만 제시하며, 기존 사후 분석이나 격리 환경 재현으로 확인된 경우에만 `확정`합니다.

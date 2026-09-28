# Radar Project

Cloudflare Radar 도메인 순위 이력을 누적하고, 변화 신호와 Gemini 태그를 조회하는 **개인 서버용 프로젝트 초안**입니다.

**현재 상태:** Kubernetes 웹·공개 HTTPS·Airflow 일간/주간 수집을 사용자 로그로 확인했습니다. 주간 2026-09-21 WORLD는 1,000,001개이며 `web`/`ws`도 core에 보존됐습니다. Gemini 100개 1차 분류 worker와 일일 예산·원본 보존·Airflow DAG를 추가했습니다. **Gemini 새 이미지 배포·실제 API 호출·분류 품질은 아직 서버 검증 전**입니다. 20 URL 상세 점검은 요청/결과 계약만 있습니다.

## 구성

- Python 3.12+ / uv: 입력 검증, raw Parquet 저장, PostgreSQL 적재
- PostgreSQL: `stage` → `core` → `mart`
- Next.js / React: 서버 측 DB 조회, 날짜·국가·태그 필터, 도메인 이력
- Gemini `gemini-3.1-flash-lite`: 100개 잠정 분류 worker / 20 URL 상세 점검 요청·결과 계약
- 초기 태그 44개: 분야 22개, 용도 16개, 도메인 역할 6개

```text
완료된 수집 자료(JSON 계약) → raw Parquet → stage 1차 적재
                                      → core/mart 2차 적재 → Next.js 서버 → 브라우저
                                             └→ Gemini 1차 분류 → raw Parquet
                                                               → 결과 검증·적재 → 화면
```

raw 파일은 Git에서 제외합니다. PostgreSQL을 브라우저에 직접 노출하지 않습니다. 기존 다른 프로젝트의 DB·수집 작업은 사용하거나 변경하지 않습니다.

배포 대상은 **Ubuntu 24.04의 Kubernetes, namespace `radar`**이며 웹·파이프라인 모두 Pod에서 실행합니다. 기존 PostgreSQL을 사용합니다. Git 저장소는 [BeolLe/radar-project](https://github.com/BeolLe/radar-project)입니다. GHCR에 이미지를 보관하고 **Argo CD 수동 Sync**로 웹을 배포합니다. 서버 배포는 아래 Kubernetes 절을 따르고, Docker Compose는 로컬 개발용으로만 사용합니다.

## 파일

```text
radar/                  Python CLI, Cloudflare 일간/주간 수집, 입력 계약, 태깅 검증
airflow/                기존 Airflow git-sync 저장소에 배포할 독립 DAG 파일
schema.sql              새 전용 DB용 stage/core/mart 초기 스키마
taxonomy.json           고정 태그 코드와 한국어 표시 이름
tests/                  단위 테스트 + 선택적 PostgreSQL 통합 테스트
web/                    Next.js 앱과 서버 전용 PostgreSQL 조회
compose.yaml            로컬 개발 PostgreSQL만 실행하는 선택 사항
Dockerfile.web          Next.js 컨테이너 이미지
Dockerfile.pipeline     Python CLI 컨테이너 이미지
k8s/                    웹 Deployment/Service, raw 전용 local PV/PVC, 별도 수동 Job
argocd/application.json  radar Application 등록용; 자동 동기화 없음
.env.example            비밀정보 없는 환경변수 예시
```

## 로컬 실행

Python은 uv, 웹은 Node.js 22 이상과 npm을 사용합니다. 아래 명령은 저장소 루트에서 시작합니다. Docker는 로컬 DB를 편하게 만드는 선택 사항이며 기존 개인 서버 PostgreSQL을 사용해도 됩니다.

### 1. 환경변수와 개발 DB

```sh
cp .env.example .env
cp web/.env.example web/.env.local
```

두 파일의 DB URL과 비밀번호를 같은 값으로 수정합니다. `change-me`는 예시이므로 실제 배포에는 사용하지 마세요. 비밀번호에 URL 특수문자가 있으면 DB URL에서 인코딩해야 합니다.

```sh
docker compose up -d db
uv sync --locked
uv run --env-file .env python -m radar init-db
```

`init-db`는 **새 프로젝트 전용 DB**를 대상으로 합니다. `CREATE TABLE IF NOT EXISTS`는 이후 스키마 변경용 migration이 아닙니다. 기존 운영 테이블의 업그레이드에 사용하지 마세요.

### 2. 가상 자료 적재

```sh
uv run --env-file .env python -m radar demo
# 같은 명령을 다시 실행하면 already_published이며 관측 행이 늘지 않습니다.
uv run --env-file .env python -m radar demo
```

글로벌 주간 3개 시점과 KR/JP 일간 자료를 생성합니다. 4주 시험 수집 계획과 별개인 소규모 코드 검증 자료이며 Cloudflare 실데이터가 아닙니다. 실제 수집 DB와 섞지 말고 로컬 개발 DB에서만 사용하세요.

### 3. 웹 실행

```sh
cd web
npm ci
npm run dev
```

`http://127.0.0.1:3000`에서 조회합니다. 가상 자료의 날짜는 2026-01월입니다. 도메인 앞부분 검색, 자료 종류·국가·날짜·태그 필터, 도메인별 이력을 확인할 수 있습니다. 분류 결과를 적재하지 않았다면 태그는 미분류로 표시됩니다.

DB 미연결 시 안내 화면을 보여주며 실제 자료처럼 보이는 샘플로 자동 대체하지 않습니다. 일간/주간 전환 후 해당 자료의 날짜를 다시 선택할 수 있습니다. 도메인 이력은 선택한 종류·국가의 관측된 날짜만 표시합니다.

## 외부 수집기의 입력 계약

이 초안의 `ingest`는 Cloudflare API 원본 응답을 바로 받지 않습니다. 수집 명령이 응답을 아래 계약으로 변환한 후 기존 `ingest`를 호출합니다. API의 dataset `description`은 사이트 소개문이 아닙니다.

일간 POPULAR 예시:

```json
{
  "kind": "daily",
  "date": "2026-01-19",
  "location": "KR",
  "sources": [{
    "id": "example-source-id",
    "expected_rows": 2,
    "rows": [
      {"domain": "alpha.example", "rank": 1},
      {"domain": "beta.example", "rank": 2}
    ]
  }]
}
```

주간 입력은 `kind=weekly`, `location=WORLD`입니다. 수집기는 `200`, `500`, `1000`, `2000`, `5000`, `10000`, `20000`, `50000`, `100000`, `200000`, `500000`, `1000000`의 12개 bucket을 모두 요구합니다. 이전 4개 bucket 입력과 raw 파일도 계속 읽습니다. 각 파일 내부 중복은 거부하고, 파일 간 누적 포함 관계를 검사한 뒤 도메인별 가장 작은 bucket으로 합칩니다. 전체 원본은 1,888,700행, 정합성 검증 후 core 관측은 1,000,000행입니다.

```sh
uv run python -m radar demo-input > demo-input.json
uv run --env-file .env python -m radar ingest demo-input.json
```

`expected_rows`는 수집기가 확인한 소스 계약입니다. 받은 행 수를 그대로 넣으면 부분 수집 검증이 되지 않습니다. 국가별 자료가 반드시 100행이라고 가정하지 않으며, 실제 소스의 완료 근거가 필요합니다. `date`는 실행일이 아니라 원본 관측 기준일입니다. 주간 파일들이 같은 기간인지 확인하는 책임은 수집 adapter에 있습니다.

### 적재 보장과 의도적인 제한

- raw Parquet는 입력 내용 해시 기반 파일로 보관합니다. 원본 행의 추가 필드도 JSON 문자열로 남깁니다.
- stage 적재를 먼저 commit하고 core/mart를 별도 트랜잭션으로 공개합니다. 실패한 공개 작업은 raw와 stage를 보존합니다. 성공 후 해당 batch stage만 비웁니다.
- 같은 기간·같은 내용 재실행은 중복 적재하지 않습니다. 같은 기간의 다른 내용은 자동 덮어쓰기하지 않고 **revision 오류로 중단**합니다.
- 종류·국가별 날짜 순서대로 적재합니다. 이미 적재한 최신 시점보다 오래된 자료의 추가와 사후 수정은 거부합니다. 이를 지원하는 downstream mart 재계산은 후속 구현입니다.
- 첫 시점은 비교 기준입니다. 직전 달력 주/일의 완료본이 없으면 변화 신호를 계산하지 않습니다. 누락을 순위권 이탈로 판정하지 않습니다.
- `value`는 주간이면 **구간 상한**, 일간이면 **정확 순위**입니다. 공통 observation 테이블을 쓰지만 snapshot의 kind로 의미를 구분합니다.
- 최초 관측·재진입·상승·2회 연속 상승을 mart에 저장합니다. 상승은 작은 value로 이동했다는 뜻이며 주간 구간 안의 변동은 알 수 없습니다.
- `www`·서브도메인은 임의로 합치지 않습니다. 대소문자·마지막 점·IDNA만 정규화합니다.
- `ponytail:` 주석처럼 한 입력을 메모리에 올리고 적재 writer를 직렬화합니다. 전수 자료로 메모리 실측 후 필요할 때 스트리밍으로 교체합니다.

## 두 단계 태깅

### 화면 표시와 갱신 — web:v0.2.0

목록과 도메인 상세 화면은 15초마다 현재 경로를 다시 조회합니다. 숨긴 탭 및 검색 입력 중에는 자동 갱신을 쉬고, `지금 업데이트` 버튼으로 직접 확인할 수 있습니다. DB 적재 후 다음 갱신에 반영되는 polling 방식이지 WebSocket 즉시 전송은 아닙니다. DB 오류를 성공 또는 최신 확인으로 표시하지 않습니다.

결과가 없는 도메인은 `태그 수집·분류 대기`, unknown은 `분류 보류 · 근거 부족`, fetch_failed는 `URL 조회 실패 · 재점검 필요`로 표시합니다. 이는 개별 도메인의 결과 상태이며 실제 worker가 지금 실행 중임을 보증하는 상태판은 아닙니다. 목록에서는 기존 성공 결과가 있으면 계속 우선 표시하고, 상세 이력에서 후속 실패/보류를 확인할 수 있습니다.

기존 `role.*` 태그를 목록의 **도메인 역할** 열에 따로 표시합니다. 사용자용 사이트·앱 / API·백엔드 / CDN·정적 리소스 / 광고·측정 / 인증·서비스 연동 / 주차·판매 도메인을 재사용하며 taxonomy 변경이나 pipeline 이미지 재빌드는 필요 없습니다. `api`, `app` 문자열만으로 역할을 강제하지 않습니다. 앱 자체와 앱의 API는 다른 역할일 수 있고, 현재 분류체계는 사용자용 웹사이트와 앱을 하나의 역할로 묶습니다.

검사: `cd web && npm test && npm run typecheck && npm run build`. [Next.js router.refresh 문서](https://nextjs.org/docs/app/api-reference/functions/use-router)에 따라 동적 페이지의 DB 결과를 갱신하며 현재 URL의 필터·페이지를 유지합니다. 배포는 web:v0.2.0 빌드·push 확인 후 웹 Deployment 이미지만 변경합니다.

### 요청 준비 — API 호출 없음

```sh
uv run --env-file .env python -m radar prepare-tags --phase preliminary --limit 100 > preliminary-request.json
uv run --env-file .env python -m radar prepare-tags --phase detail --limit 20 > detail-request.json
```

요청에는 도메인 ID·이름·URL·44개 허용 코드·프롬프트 규칙이 들어갑니다. 도메인별 1차 완료와 상세 점검 완료를 별도로 취급하므로 1차 태그가 있어도 detail 대상입니다.

`prepare-tags` 자체는 offline 요청 초안입니다. 실제 1차 worker는 아래 `tag-batch` / `tag-pending`을 사용합니다. 상세 URL Context 자동 실행·초기 모집단 고정·1차 종료 후 2차 전환·별도 서비스 상세정보 테이블은 아직 구현하지 않았습니다. unknown 이력은 자동 무한 재시도하지 않으며 재점검 정책도 후속 구현입니다.

### 실제 1차 분류 — pipeline:v0.4.0

CLI 직접 실행은 `GEMINI_API_KEY`, `DATABASE_URL`, 영속 `RADAR_DATA_DIR`가 필요합니다. Airflow 운영은 기존 Variable **`gemini_api_key`**를 태스크 실행 시 읽고, Pod 시작 콜백에서 인증된 Kubernetes attach 표준입력 스트림으로 전달합니다. Gemini용 Secret은 만들지 않습니다. 키를 Pod spec·명령 인자·템플릿·XCom·파일에 넣지 않으며 Pod 프로세스 메모리의 환경변수로만 사용합니다. 기존 44개 태그 계약과 DB를 재사용하므로 스키마 변경 및 웹 이미지 교체는 없습니다.

```sh
# 첫 검증: 100개 × 1회. API 키를 명령 인자에 넣지 않습니다.
uv run --env-file .env python -m radar tag-batch --batch-id preliminary-check-001 --limit 100
# 자동 worker와 동일한 호출: 최대 200회, 호출 사이 60초 대기
uv run --env-file .env python -m radar tag-pending --run-id preliminary-run-001 --max-requests 200
```

- `gemini-3.1-flash-lite`의 `generateContent` JSON Schema 응답을 사용합니다. Steam의 모델/키 환경변수 규약을 재사용하며, URL Context가 없는 1차 분류라 Interactions 호출 코드를 복사하지 않습니다. 도메인 ID당 결과 한 개, 허용 태그 최대 6개, 신뢰도 0..1을 검사합니다. 누락·잘림·잘못된 태그는 해당 배치 전체 적재를 거부합니다.
- 대상은 preliminary 이력이 없는 도메인만입니다. **누적 국가 관측 이력에서 한국 → 다른 국가 → 나머지 글로벌 전용 도메인** 순서이며 여러 국가에 겹치면 한국 우선으로 한 번만 처리합니다. 같은 그룹은 관측 최소 순위·도메인 ID 순서입니다(글로벌 전용은 ID 순서). `web`/`ws`도 삭제하지 않고 근거가 부족하면 unknown으로 기록합니다. 사이트를 실제 방문했다는 의미가 아닙니다.
- DB advisory lock으로 Radar worker 호출을 직렬화합니다. 요청 전 예산을 예약하고, raw/gemini 아래 요청·API 원문·응답 시각을 Parquet로 보존한 다음 기존 DB importer를 호출합니다. 응답의 실제 modelVersion·사용 토큰은 raw와 tag_result.evidence.api에 저장합니다.
- 같은 batch-id/run-id 재실행은 저장된 응답을 재사용합니다. 응답 저장 전에 종료된 배치는 자동 재호출하지 않습니다. 원인 확인 후 새 ID로 재요청해야 하며, 이전 호출 예산은 환급하지 않습니다. raw/PVC를 삭제하거나 바꾸면 이 보호가 사라집니다.
- 일일 예산은 미국 태평양 날짜 기준 최대 200회이며, 실패/수동 검증도 포함합니다. 이 장부는 **Radar만** 셉니다. 같은 Google 프로젝트의 다른 앱 사용량은 알 수 없으므로 다시 Steam을 가동하면 예산을 나눠야 합니다. 429/네트워크 오류는 즉시 중단하며 자동 재시도하지 않습니다.
- `airflow/radar_tagging.py`: 매일 18:00 KST, 최초 paused, retries=0, 기존 radar_collection pool 재사용. 수집 DAG와 직접 성공 의존성은 없고 실행 시 DB에 이미 적재된 도메인을 처리합니다. 첫 수동 Trigger의 `max_requests`는 **1**로 설정하고 결과 확인 후 스케줄을 활성화합니다. 모든 1차 대상 처리 후에는 추가 API 호출 없이 종료합니다.

배포 순서: v0.4.0 이미지 빌드/테스트/GHCR push → 갱신한 radar RBAC 적용 → Airflow git-sync 저장소에 `radar_tagging.py` 추가 → DAG processor import 확인 → 1회 검증 → 활성화. 기존 Airflow Variable `gemini_api_key`를 그대로 사용하고 수집 DAG 이미지는 바꾸지 않습니다. 키 전달에는 `pipeline/airflow-worker`의 radar namespace **pods/attach get** 권한만 추가합니다. secrets 조회/생성·pods/exec 권한은 추가하지 않습니다. 전달 실패 시 300초 안에 수신 대기가 끝나며 오류는 키 없이 출력합니다. Kubernetes 관리자·프로세스 메모리를 읽을 수 있는 운영자는 이 방식에서도 신뢰 경계 안에 있습니다.

```sh
kubectl apply -f k8s/airflow-rbac.json
kubectl auth can-i get pods/attach -n radar --as=system:serviceaccount:pipeline:airflow-worker
```

이 변경은 DAG와 RBAC만으로 기존 v0.4.0 이미지에 적용할 수 있습니다. 기존 Secret 방식으로 되돌리지 않고 태깅 DAG를 pause하면 키 전달도 중단됩니다. Variable 키를 바꾸면 다음 새 태깅 Pod부터 적용되며 이미 실행 중인 Pod에는 소급되지 않습니다.

근거: [모델 명세](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite), [구조화 응답](https://ai.google.dev/gemini-api/docs/structured-output), [프로젝트 단위 한도·태평양 자정 초기화](https://ai.google.dev/gemini-api/docs/rate-limits). 200회는 이 프로젝트의 운영 상한이며 Google이 모든 계정에 보장하는 한도가 아닙니다.

### 결과 검증·적재

향후 상세 점검 adapter는 API 응답을 다음 내부 형식으로 변환해야 합니다. `tool_evidence`는 **모델이 쓴 JSON이 아니라 URL Context 도구 메타데이터에서** 추출해야 합니다. 이 파일 입력만으로 웹을 실제 조회했다고 독립 검증할 수는 없습니다.

```json
{
  "phase": "detail",
  "checked_at": "2026-09-27T00:00:00+00:00",
  "results": [{
    "domain_id": 1,
    "status": "classified",
    "tags": [{"code": "topic.it", "confidence": 0.8}],
    "reason": "조회한 페이지에 기술 서비스 설명이 있음"
  }],
  "tool_evidence": {
    "1": {"url": "https://alpha.example", "status": "success"}
  }
}
```

위 ID·도메인은 실제 요청 초안과 일치하도록 바꿔야 합니다. preliminary 결과는 tool_evidence 없이 저장할 수 있습니다. unknown은 tags가 빈 배열이어야 합니다. detail의 classified는 같은 호스트의 성공한 URL 근거와 비어 있지 않은 reason이 필요합니다. 리다이렉트로 호스트가 바뀌는 경우 이 초안은 보수적으로 거부하며, 추후 adapter가 요청 URL·최종 URL 연결을 검증하도록 보강해야 합니다.

```sh
uv run --env-file .env python -m radar import-tags detail-request.json detail-response.json
```

정의되지 않은 태그, 중복 ID, 잘못된 confidence, 요청과 다른 도메인은 거부합니다. 응답에서 빠진 ID는 완료 처리하지 않고 출력합니다. 상세 결과는 잠정 태그를 삭제하지 않으며 화면에서는 성공한 상세 결과를 우선합니다. confidence는 모델의 자기평가이지 정답 확률이 아닙니다.

프로젝트의 계획값인 하루 200요청을 기준으로 100개 × 200요청은 최대 2만 개/일, 20 URL × 200요청은 최대 4천 개/일입니다. 실제 계정 한도는 실행 전에 확인해야 합니다. 재시도·토큰 한도·다른 작업과 공유하는 API 프로젝트 사용량은 별도입니다. 100만 개의 순차 전수 처리는 이상적 가정에서도 50일 + 250일이며, 초기 서비스는 잠정 결과부터 제공합니다.

## 검증

로컬 단위·배포 계약 검사에는 Parquet 왕복/이전 해시 호환, namespace·PV·Argo CD 정책, 일간/주간 API 계약과 부분 실패 처리, Airflow DAG 설정·RBAC 범위가 포함됩니다. DAG 설정 검사는 실제 Airflow import 검사를 대체하지 않습니다. Next.js 타입 검사·프로덕션 빌드는 기존 초안에서 통과했으며 이번 변경에는 웹 수정이 없습니다.

2026-09-28 Mac 로컬의 합성 100만 도메인 검사에서 12개 bucket의 1,888,700행 검증·Parquet 저장·재읽기·해시 검증에 약 26.1초, 최대 RSS 약 549.4MiB를 관측했습니다. 규칙적인 가상 도메인이므로 실제 압축률·Ubuntu 성능 추정치가 아닙니다. 네트워크·PostgreSQL 적재도 제외합니다. 선택 실행: `RADAR_SCALE_TEST=1 uv run python -m unittest discover -s tests -p test_pipeline.py -v`.

사용자 제공 Ubuntu 실행 로그에서 **컨테이너 이미지 2개 빌드, 단위 검사 8개, `radar_test` DB 통합 검사 1개**, GHCR 이미지 2개 `v0.1.0` 업로드 및 raw PV/PVC `Bound`를 확인했습니다. 이후 `schema_ready`, 웹 Pod `1/1 Running`, 내부·공개 HTTPS의 `ready=true`와 HTTP 200도 확인했습니다. 웹은 최초 비밀번호 인증 오류 후 Secret 재입력·재시작으로 정상화됐습니다. 수집기 `v0.2.0`의 빌드·검사·push, WORLD/KR 일간 각 100건 적재와 raw 경로 출력, 사용자의 화면 확인까지 완료했습니다. **새 v0.3.0 전체 수집의 서버 성능·실행 및 Gemini 실제 호출은 미검증**입니다.

```sh
uv run python -m unittest discover -s tests -v
cd web
npm run typecheck
npm run build
```

PostgreSQL 통합 테스트는 **비어 있는 폐기 가능한 `radar_test` DB**에서만 실행합니다. 기존 DB를 지우지 않으며 기존 core.snapshot이 있으면 중단합니다. DB 생성은 직접 하고 다음 변수로 지정합니다.

```sh
RADAR_TEST_DATABASE_URL='postgresql://USER:PASSWORD@127.0.0.1:PORT/radar_test' \
  uv run python -m unittest discover -s tests -v
```

통합 검사는 raw Parquet, 관측 행 수, 재실행, 변화 신호, revision 거부, 태그 상세 결과 우선순위를 확인합니다. DB 변수가 없으면 이 검사만 skip됩니다.

## Kubernetes 배포 — GHCR + Argo CD 수동 Sync

**이 절의 명령은 서버에서 사용자가 실행할 절차입니다. 코드 업로드만으로 배포되지는 않습니다.** Application은 기존 Argo CD의 `argocd` namespace에 등록하고 앱 리소스는 `radar` namespace에 둡니다. 자동 동기화·자동 삭제·self-heal·이미지 자동 갱신은 설정하지 않았습니다. GitHub Actions는 없으며 수집 스케줄은 CronJob 대신 Airflow로 관리합니다. JSON은 Kubernetes가 기본 지원하는 manifest 형식입니다.

**첫 Sync 전 준비:** GHCR 이미지와 pull 권한, DB·조회 권한, `radar` namespace의 DB·GHCR Secret, 6절의 전용 터널 Secret `radar-tunnel`, 아래 raw 디렉터리 준비가 모두 필요합니다. StorageClass 없이 `ronny` 노드의 `/mnt/data/ronny-project/radar-data`를 사용하는 정적 local PV로 설정했습니다. 사용자 출력에서 PV/PVC `Bound`와 최초 수집의 raw 쓰기를 확인했습니다. 예전 `radar-project` namespace에 리소스를 이미 배포했다면 이름 변경은 데이터 이전이 아니므로 별도 이전 계획 없이 기존 namespace/PVC를 삭제하지 않습니다.

### 1. 서버에서 코드 받기와 사전 확인

```sh
git clone https://github.com/BeolLe/radar-project.git
cd radar-project
kubectl config current-context
kubectl get nodes -o wide
kubectl get svc -A
kubectl get storageclass
```

확인할 것은 기존 PostgreSQL의 **읽기·쓰기 primary Service 이름, namespace, 포트, TLS 방식**, 노드 아키텍처, 사용할 StorageClass입니다. PostgreSQL Pod IP나 읽기 전용 replica Service에 연결하지 않습니다. Service 주소 예시는 `PG_SERVICE.PG_NAMESPACE.svc.cluster.local:5432`이며 실제 클러스터 DNS 설정에 맞춰 바꿉니다. 기존 NetworkPolicy가 있다면 Radar namespace에서 DB·DNS로 필요한 연결을 허용해야 합니다.

### 2. 새 전용 DB와 계정 준비

기존 PostgreSQL 관리자 접속에서 **새 Radar DB를 만들 때만** 실행하는 예시입니다. 이미 같은 이름의 DB/계정이 있으면 먼저 용도를 확인합니다. 비밀번호는 `psql`의 대화형 입력을 쓰고 SQL·Git에 남기지 않습니다.

```sql
CREATE ROLE radar_owner LOGIN;
\password radar_owner
CREATE ROLE radar_reader LOGIN;
\password radar_reader
CREATE DATABASE radar OWNER radar_owner;
REVOKE ALL ON DATABASE radar FROM PUBLIC;
GRANT CONNECT ON DATABASE radar TO radar_owner, radar_reader;
```

파이프라인은 `radar_owner`, 웹은 `radar_reader`를 사용합니다. 아래 init-db Job이 끝난 뒤 **radar DB에 접속한 관리자/owner**로 조회 권한을 부여합니다.

```sql
GRANT USAGE ON SCHEMA core, mart TO radar_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA core, mart TO radar_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE radar_owner IN SCHEMA core, mart
  GRANT SELECT ON TABLES TO radar_reader;
```

### 3. 이미지 빌드·등록

아래는 **노드와 같은 아키텍처에서 Docker로 빌드하고 GHCR에 로그인한 환경**의 예시입니다. Kubernetes가 containerd를 사용한다는 사실만으로 Docker 빌드 명령을 쓸 수 있는 것은 아닙니다. 빌드 환경이 없으면 먼저 마련하거나 기존 이미지 빌드 경로를 사용하세요. Mac에서 빌드할 경우 서버 아키텍처를 확인하고 buildx의 `--platform`을 지정해야 합니다.

```sh
docker build -f Dockerfile.pipeline -t ghcr.io/beolle/radar-project-pipeline:v0.1.0 .
docker build -f Dockerfile.web -t ghcr.io/beolle/radar-project-web:v0.1.0 .
docker push ghcr.io/beolle/radar-project-pipeline:v0.1.0
docker push ghcr.io/beolle/radar-project-web:v0.1.0
```

Ubuntu에서 검증한 `radar-pipeline:test`, `radar-web:test`가 남아 있고 앱 코드·Dockerfile이 같으면 재빌드 대신 해당 이미지에 위 GHCR 태그를 붙여 push해도 됩니다. Kubernetes namespace와 Argo CD 설정만 바꾼 경우에는 앱 이미지 재빌드가 필요하지 않습니다. Git 저장소 공개 여부와 GHCR 패키지 공개 여부는 별개입니다. **Radar 이미지는 비공개로 운영하며**, 웹과 파이프라인의 `imagePullSecrets`는 namespace `radar`의 `radar-ghcr` Secret을 참조합니다. 실제 두 패키지의 visibility가 `Private`인지 GitHub에서 확인하세요. 이미 공개한 패키지는 비공개로 되돌릴 수 없으므로 별도 대응이 필요합니다. 토큰은 Dockerfile, build argument, Git에 넣지 마세요.

#### 비공개 이미지 다운로드 자격증명 준비

BeolLe 계정의 Personal access token **(classic)**을 별도로 만들고 `read:packages` 권한만 부여합니다. 이 토큰에 두 패키지의 읽기 권한이 있어야 합니다. 다운로드 전용이므로 `write:packages`는 필요하지 않습니다. 만료 전에 토큰과 Secret을 교체해야 하며, 서버의 기존 `docker login` 정보가 Kubernetes에 자동 전달되지는 않습니다.

아래는 namespace `radar`가 준비된 **Ubuntu 서버에서 최초 1회** 실행합니다. 로그인 비밀번호 프롬프트에 토큰을 입력합니다. 기존 Docker 설정 전체를 복사하지 않고 임시 디렉터리에 GHCR 인증만 만듭니다. Secret이 이미 존재하면 덮어쓰지 않고 실패하므로 기존 용도를 먼저 확인합니다. 종료 시 이번에 만든 임시 인증 파일만 삭제하며 서버의 기존 Docker 로그인은 유지됩니다.

```bash
(
  set +x
  set -eu
  umask 077
  RADAR_AUTH_DIR=$(mktemp -d)
  trap 'rm -f "$RADAR_AUTH_DIR/config.json"; rmdir "$RADAR_AUTH_DIR"' EXIT
  docker --config "$RADAR_AUTH_DIR" login ghcr.io -u BeolLe
  kubectl -n radar create secret generic radar-ghcr \
    --type=kubernetes.io/dockerconfigjson \
    --from-file=.dockerconfigjson="$RADAR_AUTH_DIR/config.json"
)
kubectl -n radar get secret radar-ghcr
```

토큰이나 `kubectl get secret -o yaml/json` 출력은 공유하지 마세요. Secret 생성 성공은 실제 Pod의 이미지 다운로드 성공을 증명하지 않습니다. Secret 내용은 Git/Argo CD 관리 대상에 넣지 않습니다.

업데이트 때는 `git pull --ff-only` 후 **새 태그**로 빌드·등록하고 manifest의 태그도 바꿉니다. 이미 배포한 `v0.1.0`을 덮어쓰지 않습니다. 되돌릴 때는 이전 이미지 태그로 복구하되 DB 스키마 변경의 호환성은 별도로 확인합니다.

### 4. Secret·raw 볼륨 준비

먼저 **Ubuntu의 `ronny` 노드에서만** 실행해 데이터 디렉터리를 준비합니다. 파이프라인 컨테이너의 UID/GID `10001:10001`에 맞춥니다. 이미 경로가 있거나 심볼릭 링크라면 기존 내용을 확인하기 전에는 소유권을 바꾸지 않습니다.

```sh
if [ -e /mnt/data/ronny-project/radar-data ] || [ -L /mnt/data/ronny-project/radar-data ]; then
  echo '이미 경로가 있습니다. 용도와 소유권을 확인한 뒤 진행하세요.'
  ls -ld /mnt/data/ronny-project/radar-data
else
  sudo install -d -o 10001 -g 10001 -m 2770 /mnt/data/ronny-project/radar-data
fi
```

연결은 `radar-raw-pv` → namespace `radar`의 PVC `radar-raw` → 파이프라인 `/data` 순서입니다. 따라서 실제 Parquet는 서버의 `/mnt/data/ronny-project/radar-data/raw/`에 쌓입니다. `local` 볼륨과 `metadata.name=ronny` node affinity로 다른 노드의 같은 경로를 사용하지 않게 했습니다. 파일·디스크가 해당 노드에 있으므로 노드 장애 시 자동으로 다른 노드에서 복구되지 않습니다.

```sh
kubectl apply -f k8s/namespace.json
```

저장소 밖의 접근 제한된 파일 두 개를 준비합니다. 각 파일에는 `DATABASE_URL=postgresql://...` 한 줄만 넣습니다. 파이프라인 파일은 owner, 웹 파일은 reader 계정을 사용합니다. TLS 연결 옵션은 기존 PostgreSQL 정책을 따르고 검증을 무조건 끄지 않습니다. URL의 비밀번호 특수문자는 인코딩합니다. 아래 파일명은 예시이며 자신의 실제 경로로 바꿉니다.

```sh
chmod 600 /secure/path/radar-pipeline.env /secure/path/radar-web.env
kubectl -n radar create secret generic radar-pipeline-db --from-env-file=/secure/path/radar-pipeline.env
kubectl -n radar create secret generic radar-web-db --from-env-file=/secure/path/radar-web.env
kubectl apply -f k8s/pv.json
kubectl apply -f k8s/storage.json
kubectl get pv radar-raw-pv
kubectl -n radar get pvc radar-raw
```

이 PV/PVC는 `storageClassName: ""`, PVC의 `volumeName`, PV의 `claimRef`로 서로 지정되어 있습니다. StorageClass 설치나 자동 볼륨 생성이 필요하지 않으며 기존 PostgreSQL·다른 프로젝트 PV를 사용하지 않습니다. 초기화 Job 전에 양쪽이 `Bound`인지 확인합니다. 이미 다른 spec으로 만든 PVC가 있다면 강제로 삭제·재생성하지 말고 먼저 현재 연결을 확인합니다.

**10Gi는 초기 검증용 용량 선언이며, 1년 보관 산정치나 디스크를 잘라 예약하는 설정이 아닙니다.** 일반 디렉터리를 사용하는 이 구성에는 10Gi 쓰기 차단 quota가 없으므로 같은 파일시스템의 남은 용량을 감시해야 합니다. PV 이름·경로·node affinity 변경이나 용량 증설을 단순 이미지 업데이트처럼 적용하지 않습니다.

PV는 `Retain`이고 Namespace·PV·PVC에는 Argo CD의 `Prune=false,Delete=false`를 설정했습니다. Argo CD 정리 과정에서 스토리지를 의도치 않게 지우지 않도록 한 것이며, `kubectl delete`, 파일 직접 삭제, 디스크 고장까지 막는 백업은 아닙니다. Namespace/PVC/PV 삭제를 초기화 명령처럼 사용하지 않습니다. 초기 수동 준비 이후에는 이 리소스 정의도 Argo CD가 같은 Git 설정을 관리합니다. 기존 다른 Application이 같은 리소스를 관리한다면 소유권부터 확인합니다.

Secret은 저장소 밖에 둔다는 것만으로 안전이 완성되지 않습니다. 클러스터의 RBAC·저장 시 암호화 정책도 확인해야 합니다. 웹에는 API 키·쓰기 DB 계정을 전달하지 않습니다.

### 5. 수동 초기화 Job → 조회 권한 → 웹

`pipeline-job.json` 기본 명령은 **init-db**입니다. 지정된 DB에 스키마를 생성하므로 새 전용 DB URL을 먼저 확인하세요. 자동 재시도는 껐으며 실패하면 로그를 확인한 뒤 재실행합니다. Job은 generateName을 사용하므로 apply가 아니라 create로 실행합니다.

```sh
RADAR_JOB=$(kubectl create -f k8s/pipeline-job.json -o name)
kubectl -n radar wait --for=condition=complete --timeout=1800s "$RADAR_JOB"
kubectl -n radar logs "$RADAR_JOB"
```

완료 후 2절의 reader 조회 권한을 부여합니다. 초기화 Job은 `k8s/kustomization.yaml`에 포함하지 않았으므로 웹 Sync가 DB 초기화를 실행하지 않습니다. 이미 초기화한 테스트 DB를 그대로 검증할 때는 init-db를 다시 실행할 필요가 없습니다.

준비가 끝나면 기존 클러스터에서 **Application 등록 파일만** 적용합니다. 별도 GitOps 저장소나 기존 root-apps는 변경하지 않습니다.

```sh
kubectl apply -f argocd/application.json
kubectl -n argocd get application radar
```

이 등록만으로 웹은 배포되지 않습니다. Argo CD 화면에서 `radar`의 대상이 `radar-project.git` / `main` / `k8s`, destination namespace가 `radar`인지 확인합니다. Diff를 검토한 뒤 **수동 Sync**를 실행하며 Prune는 선택하지 않습니다. Application에 자동 동기화 정책과 cascade 삭제 finalizer를 넣지 않았고 스토리지 리소스는 별도 삭제 방지 옵션이 있습니다. 기존 AppProject `default` 정책에서 Namespace·PersistentVolume 같은 cluster-scoped 리소스를 금지한다면 이 앱만을 위해 전역 정책을 임의로 풀지 말고 권한·관리 경계를 먼저 결정합니다.

Sync 이후 확인:

```sh
kubectl -n radar rollout status deployment/radar-web --timeout=180s
kubectl -n radar port-forward service/radar-web 3000:80
```

이후 배포 변경은 Git에 반영하고 수동 Sync로 적용합니다. 정상 운영에서 같은 웹 리소스를 Argo CD와 `kubectl apply -k k8s`로 번갈아 수정하지 않습니다. 등록한 Application 자체는 별도 bootstrap 파일이므로 해당 파일 변경은 다시 apply해야 합니다.

port-forward를 실행한 컴퓨터의 `http://127.0.0.1:3000`에서 확인합니다. 원격 Ubuntu에서 실행했다면 Mac/모바일의 localhost가 아닙니다. 공개 접속은 아래 전용 Cloudflare Tunnel을 사용하며 웹 Service는 ClusterIP로 유지합니다.

`/api/ready`는 DB·조회 테이블 접근 실패 시 503을 반환합니다. DB 장애는 readiness만 실패시키며 liveness는 웹 프로세스 응답 여부를 따로 확인합니다. 아직 자료가 없는 빈 DB도 스키마와 권한이 정상이면 ready입니다.

### 6. Radar 전용 Cloudflare Tunnel

공개 주소는 **https://radar.selfronny.com**입니다. `k8s/cloudflared.json`은 namespace `radar`에 `radar-cloudflared` Deployment를 추가합니다. 기존 공개용 터널과 내부 WARP·로드밸런서는 변경하지 않습니다. 새 터널은 원격 관리 방식이며, **Kubernetes 실행 설정은 Git/Argo CD, 호스트명→서비스 경로는 Cloudflare 대시보드, 토큰은 Kubernetes Secret**에서 관리합니다.

1. Cloudflare 대시보드의 **Networking → Tunnels → Create a tunnel**에서 `radar` 전용 Cloudflared 터널을 만듭니다. 같은 이름이 이미 있으면 기존 용도를 확인하고 임의로 수정하지 않습니다. 기존 터널 토큰을 재사용하면 별도 터널이 아니라 기존 터널의 추가 connector가 되므로 새 터널의 토큰을 사용합니다.
2. 설치 안내에서 Docker를 선택하고 **토큰 문자열만 복사**합니다. 표시된 Docker 실행/서비스 설치 명령은 실행하지 않습니다. 실제 실행은 Kubernetes가 담당합니다. 토큰은 채팅·Git에 넣지 않습니다.
3. Ubuntu에서 아래 Secret을 만든 뒤 Argo CD를 Sync합니다. 아직 Secret이 없다면 Sync를 먼저 하지 않습니다. `read`는 Bash 숨김 입력이며 토큰에 줄바꿈을 붙이지 않습니다. 기존 Secret은 덮어쓰지 않습니다.

```bash
(
  set +x
  set -euo pipefail
  IFS= read -r -s -p 'Radar tunnel token: ' RADAR_TUNNEL_TOKEN
  printf '\n'
  [ -n "$RADAR_TUNNEL_TOKEN" ] || exit 1
  printf '%s' "$RADAR_TUNNEL_TOKEN" |
    kubectl -n radar create secret generic radar-tunnel --from-file=token=/dev/stdin
  unset RADAR_TUNNEL_TOKEN
)
kubectl -n radar get secret radar-tunnel
```

4. 기존 Argo CD `radar` 앱을 Refresh하고 Diff를 확인한 뒤 **수동 Sync**합니다. Prune·Force·Auto-Sync는 켜지 않습니다. 터널은 Pod 1개이며 롤링 업데이트 중에는 일시적으로 2개가 될 수 있습니다. 단일 노드/단일 replica 구성은 무중단·고가용성을 보장하지 않습니다. 초기 자원값은 request 50m/64Mi, limit 500m/256Mi입니다.
5. 아래 상태를 확인합니다. `/ready`는 Cloudflare 연결을 검사하고, liveness는 로컬 metrics TCP만 검사하여 외부망 장애 때 재시작을 반복하지 않게 했습니다. Cloudflare로 나가는 연결과 클러스터 DNS·웹 서비스 접근이 가능해야 합니다.

```bash
kubectl -n radar rollout status deployment/radar-cloudflared --timeout=180s
kubectl -n radar get pods -l app=radar-cloudflared
kubectl -n radar logs deployment/radar-cloudflared --tail=50
```

6. 새 터널의 **Routes → Add route → Published application**에서 아래 경로를 추가합니다. 화면에 `Public Hostname`으로 표시되는 경우 같은 공개 호스트명 설정을 사용합니다. 이미 `radar.selfronny.com` DNS/경로가 있으면 용도를 확인하기 전에는 덮어쓰지 않습니다.

| 항목 | 값 |
|---|---|
| Subdomain | `radar` |
| Domain | `selfronny.com` |
| Path | 비움 |
| Service type | `HTTP` |
| Service URL | `radar-web.radar.svc.cluster.local:80` |

전체 원본 URL은 `http://radar-web.radar.svc.cluster.local:80`입니다. 방문자는 HTTPS를 사용하며 공개 주소 자체를 원본 URL에 넣지 않습니다. 이 터널에 DB·metrics 주소나 사설망 경로를 추가하지 않습니다. 별도 LoadBalancer/NodePort/Ingress와 공유기 인바운드 포트 개방은 필요하지 않습니다. 공개 웹이므로 방문자의 WARP 연결이나 Access 로그인을 요구하도록 새 정책을 만들지 않습니다. 기존 와일드카드 Access 정책이 적용되는 경우 확인하되 다른 서비스 정책을 일괄 해제하지 않습니다.

7. 외부망 브라우저에서 화면을 확인하고 아래 응답도 확인합니다. 토큰 변경 시 Secret 갱신 후 터널 Pod를 재시작해야 합니다. 철회할 때는 해당 공개 경로를 먼저 비활성화하고 Git에서 터널 배포 제외 여부를 결정하며 기존 터널은 건드리지 않습니다.

```bash
curl --fail-with-body --max-time 15 https://radar.selfronny.com/api/ready
curl -sS --max-time 15 -o /dev/null -w 'Dashboard HTTP %{http_code}\n' https://radar.selfronny.com/
```

### 7. 첫 실데이터 수집 — 글로벌·한국 일간 Top 100

`collect-daily`는 공식 `/radar/ranking/top`의 `POPULAR` 데이터를 가져와 기존 raw Parquet → stage → core/mart 경로로 적재합니다. 첫 Job은 `WORLD`, `KR` 두 목록(관측 200행, 서로 겹치는 도메인은 core.domain에서 통합)을 대상으로 하며 주간 100만 도메인 적재가 아닙니다. 신규 의존성 없이 Python 표준 HTTP 라이브러리를 사용합니다.

- 실행일을 관측일로 쓰지 않고 `result.meta.top_0.date`를 사용합니다. 첫 응답의 날짜로 나머지 국가 요청을 고정하고 다른 날짜로 대체된 응답은 거부합니다. `--date YYYY-MM-DD`로 정확한 원본 날짜를 지정할 수도 있습니다.
- 첫 구현은 각 목록이 정확히 100행이고 순위가 1~100, 도메인이 중복되지 않는 경우만 적재합니다. 실제로 100개 미만을 제공하는 국가도 있을 수 있지만 완료 근거를 확인하기 전에는 받은 행 수로 기준을 낮추지 않습니다.
- 요청한 목록을 모두 받아 검증하기 전에는 DB 적재를 시작하지 않습니다. HTTP 인증 오류·잘못된 응답은 중단합니다. 429/일부 5xx/연결 오류는 최대 3회 시도하며 30초를 넘는 Retry-After는 기다리는 대신 중단합니다. 자동 무한 재시도나 날짜 fallback은 없습니다.
- raw에는 제공된 각 순위 행의 추가 필드(Cloudflare categories 포함)를 보존합니다. 원본 HTTP 봉투 전체를 복제하지는 않습니다. 응답의 변경 시각/전송 메타데이터는 해시에 넣지 않아 같은 날짜·내용의 재수집은 `already_published`로 처리할 수 있습니다. 기준일·location·endpoint·POPULAR 종류는 보존합니다. Cloudflare categories를 Gemini 태그로 기록하지 않습니다.
- **DB 트랜잭션은 국가/목록별입니다.** 적재 도중 DB 오류가 나면 앞 목록은 완료됐을 수 있습니다. 상태를 확인하고 같은 날짜로 재실행하면 동일 내용은 중복 적재하지 않습니다. 이미 공개한 동일 날짜의 내용이 바뀌면 덮어쓰지 않고 중단합니다. 과거 backfill은 기존 구현처럼 시간 역순 삽입을 허용하지 않습니다.

#### API 토큰과 Secret

Cloudflare의 **Custom API Token → Account → Radar → Read** 권한을 사용합니다. Tunnel token, GHCR 토큰, Global API Key와 다릅니다. 웹과 터널에는 이 토큰을 주지 않습니다. Ubuntu에서 최초 한 번 등록합니다.

```bash
(
  set +x
  set -euo pipefail
  IFS= read -r -s -p 'Cloudflare Radar API token: ' RADAR_API_TOKEN
  printf '\n'
  [ -n "$RADAR_API_TOKEN" ] || exit 1
  printf '%s' "$RADAR_API_TOKEN" |
    kubectl -n radar create secret generic radar-cloudflare-api --from-file=token=/dev/stdin
  unset RADAR_API_TOKEN
)
kubectl -n radar get secret radar-cloudflare-api
```

#### 파이프라인 이미지만 새 태그로 빌드·등록

Ubuntu 저장소에서 실행합니다. 웹 이미지는 변경하지 않으며 기존 `v0.1.0`도 덮어쓰지 않습니다.

```bash
git pull --ff-only
docker build -f Dockerfile.pipeline -t ghcr.io/beolle/radar-project-pipeline:v0.2.0 .
docker run --rm --read-only --tmpfs /tmp \
  --mount "type=bind,src=$PWD/tests,dst=/tests,readonly" \
  --entrypoint python ghcr.io/beolle/radar-project-pipeline:v0.2.0 \
  -m unittest discover -s /tests -p test_cloudflare.py -v
```

빌드와 합성 API 검사 8개가 성공한 뒤, 기존 GHCR 쓰기 권한 로그인으로 업로드합니다.

```bash
docker push ghcr.io/beolle/radar-project-pipeline:v0.2.0
```

#### 수동 수집 Job과 적재 확인

`k8s/collect-job.json`은 Argo CD 자동/수동 Sync 대상에서 제외했습니다. 아래 `create`를 한 번만 실행하고 실패·시간 초과 시 먼저 해당 Job을 확인합니다. 초기화 Job을 다시 실행하거나 demo 데이터를 운영 DB에 넣지 않습니다.

```bash
RADAR_COLLECT_JOB=$(kubectl -n radar create -f k8s/collect-job.json -o name) &&
kubectl -n radar wait --for=condition=complete --timeout=180s "$RADAR_COLLECT_JOB"
kubectl -n radar get "$RADAR_COLLECT_JOB"
kubectl -n radar logs "$RADAR_COLLECT_JOB" --tail=80
```

출력의 날짜·location·rows·status·raw 경로를 확인합니다. `published` 또는 재실행의 `already_published`가 정상입니다. DB 확인은 조회만 합니다.

```bash
kubectl -n db exec -it pg-archive-postgresql-0 -- \
  psql -X -U postgres -d radar -P pager=off -v ON_ERROR_STOP=1 \
  -c "SELECT s.kind, s.period_date, s.location, count(o.domain_id) AS rows
      FROM core.snapshot s LEFT JOIN core.observation o ON o.snapshot_id=s.id
      GROUP BY s.id ORDER BY s.period_date DESC, s.location;"
```

화면은 [글로벌 일간](https://radar.selfronny.com/?kind=daily&location=WORLD) 또는 [한국 일간](https://radar.selfronny.com/?kind=daily&location=KR)을 선택합니다. 기본 화면의 주간 목록은 주간 수집 전까지 비어 있습니다. 첫 기준일은 비교 이력이 없으므로 변화 신호가 없고, Gemini 작업 전에는 미분류가 정상입니다. 위 수동 Job은 초기 검증용으로 남겨두며 Airflow 운영 중에는 함께 실행하지 않습니다.

로컬에서 환경변수를 안전하게 준비했다면 `uv run --env-file .env python -m radar collect-daily --locations WORLD KR --date YYYY-MM-DD`로도 실행할 수 있습니다. 실제 유효한 원본 날짜로 바꾸고 테스트 DB/운영 DB를 혼동하지 마세요.

### 8. 샘플 검증과 파이프라인 실행

샘플은 운영 DB가 아니라 별도 개발 DB에서 확인합니다. `pipeline-job.json`을 Git 제외 경로인 `k8s/local/`에 복사해 Secret 참조를 개발 DB 것으로 바꾸고 `args`를 `["demo"]`로 바꾸면 같은 Job 실행 절차로 샘플 적재를 검증할 수 있습니다. 실제 파일 적재는 PVC에 입력 파일을 준비한 뒤 `["ingest", "/data/input/snapshot.json"]`을 사용합니다. 운영 수집은 아래 Airflow 경로를 사용합니다.

raw는 `/data/raw`에 기록되어 Pod가 종료돼도 PVC에 남습니다. 웹에는 raw PVC를 연결하지 않습니다. Parquet는 10,000행 단위로 읽고 쓰지만 입력 계약과 검증 결과는 메모리에 유지합니다. 파이프라인 4Gi 제한은 서버 실측에 따라 조정할 시작값입니다. 수동 Job은 제한 시간 30분, Airflow 수집 Pod는 2시간입니다. CronJob은 추가하지 않습니다.

### 9. Airflow 자동 수집 연결

기존 `pipeline` namespace의 KubernetesExecutor와 `airflow-practice` git-sync를 그대로 사용합니다. Steam DAG와 Airflow 공통 설정은 변경하지 않습니다. 실제 실행 계정은 사용자 출력에서 `pipeline/airflow-worker`로 확인했습니다.

- `radar_daily`: 매일 13:00 KST, 이전 UTC 날짜의 WORLD + API 지역 목록을 순차 수집합니다.
- `radar_weekly`: 화요일 14:00 KST, 실행 기준일 이전/당일 월요일에 끝난 주간 12개 파일을 수집합니다. 해당 기간이 덜 공개됐으면 실패하며 이전 주로 대체하지 않습니다.
- 재시도는 30분 간격 2회이며 날짜는 DAG run에 고정합니다. `catchup=False`, DAG별 동시 실행 1개, 공통 `radar_collection` pool 1 slot입니다.
- KPO를 실행하는 Airflow worker Pod 1개와 `radar`의 수집 Pod 1개가 추가됩니다. 지역마다 Pod를 만들지 않습니다.
- DAG는 처음 등록할 때 일시정지 상태입니다. 최초 정상 실행을 확인한 다음 활성화합니다.

전체 지역 목록은 매번 API에서 조회합니다. 사용자 계정의 탐색 결과는 253개 **국가·지역 코드**이며, 253개 모두 순위 자료가 있다는 뜻은 아닙니다. 검증된 지역은 독립적으로 공개하고 실제 실패 목록이 있으면 로그를 남긴 뒤 task를 실패 처리합니다. 재시도 때 동일 자료는 중복 적재하지 않습니다. 영구 제외 국가 목록은 만들지 않습니다.

`v0.3.1`은 사용자 제공 2026-09-27 자료 조회 결과(AI의 87위 중복, BI의 79위 중복, AN/AP/AQ/BV/CC의 빈 결과)를 반영합니다.

- 일간 순위는 공급자 원본대로 중복·건너뜀을 허용하며 재번호를 매기지 않습니다. 100행, 도메인 중복 금지, 정수 순위 1..100, 요청 날짜 일치 검증은 유지합니다. 동순위는 도메인명으로 정렬하여 응답 순서가 바뀌어도 해시가 같습니다. 기존 중복 없는 순위의 해시는 바뀌지 않습니다.
- `success=true`, errors 없음, `top_0=[]`, `meta.top_0.date=null`, `meta.dateRange=null`의 관측된 형태만 `no_data`입니다. 해당 요청의 데이터 없음이지 영구 미지원이라는 뜻이 아닙니다. 다른 빈 응답·필드 누락·100행 미만·HTTP 오류는 계속 실패입니다.
- `no_data`는 지역별 `daily_fetch` 로그와 전체 수집 최종 요약의 `no_data` 배열에 기록합니다. DB나 Parquet에 빈 스냅샷을 만들지 않으며 기존 관측·변화 신호를 수정하지 않습니다. `collect-daily`도 같은 판정을 사용하고 로그에 기록합니다. 별도의 DB 수집 상태 테이블은 추가하지 않습니다.
- 최종 `published`는 이번 실행에서 적재 처리를 통과한 지역 수로, `already_published`도 포함합니다. `no_data`만으로 task를 실패시키지 않지만 실제 `failures`가 있으면 종료 코드 1입니다.

주간은 최근 catalog 100개 안에서 **정확히 지정된 주간**을 찾습니다. API 정렬에 기대어 첫 파일을 선택하지 않습니다. 과거 백필·100개 밖의 주차는 지원하지 않고 명시적으로 실패합니다. 각 파일은 이동하는 alias 대신 숫자 dataset ID로 다운로드하며, 제목·설명·기간 등 catalog 메타데이터를 raw에 보존합니다.

`v0.3.2` 주간 수집 수정:

- 사용자 계정의 2026-09-14~21 자료에서 Top 50만 CSV는 500,004행, Top 100만 CSV는 1,000,001행이었습니다. 초과 이유가 동순위인지는 확인되지 않았으며 순위를 임의로 부여하거나 초과분을 자르지 않습니다.
- `bucket`/catalog의 `meta.top`은 공급자 구간 기준값으로 보존하고, `expected_rows`는 실제 파싱한 행 수를 기록하여 Parquet 재읽기·정제 시 동일한 수인지 검사합니다. 기존에 정확히 N행인 payload의 해시는 바뀌지 않습니다. 실제 행 수 기록 자체가 원본 완전성을 증명하는 것은 아닙니다.
- 주간의 정상 단일 라벨(`web`, `ws` 등)을 허용하여 raw와 stage/core에 모두 남깁니다. `run.app`·API 도메인도 유지합니다. 격리·제외 테이블은 만들지 않습니다. 일간 이름 검증, URL/IP/잘못된 이름 거부, 한 파일 내 중복 거부, 전체 버킷 포함 관계 검사는 유지합니다.
- N행 미만 파일은 부분 다운로드 가능성을 배제할 수 없어 계속 실패시킵니다. 이 최소 행 수는 프로젝트의 보수적 보호 규칙이며 공식적인 완전성 보장은 아닙니다. 기존 HTTP 응답 바이트 상한도 유지하며, 상한 초과 파일을 조용히 잘라 적재하지 않습니다.
- `weekly_fetched` 로그에 구간 기준값·실제 행 수·차이를 표시합니다. 이 로그는 다운로드/파싱 완료이며 DB 적재 성공은 마지막 `published` 또는 `already_published`로 확인합니다. 동일 도메인의 여러 구간 관측은 기존대로 가장 작은 구간에 통합됩니다.
- DB 스키마·웹·태깅 기능은 변경하지 않습니다. 로컬 회귀 검사는 합성 자료 기준이고, 실제 PostgreSQL 적재·새 이미지 실행은 서버 확인이 별도로 필요합니다. 이미지 push 완료 후에만 Airflow 저장소의 DAG를 갱신합니다.

#### 이미지와 권한 준비 — Ubuntu

```bash
cd /mnt/data/ronny-project/radar-project
git pull --ff-only
docker build -f Dockerfile.pipeline -t ghcr.io/beolle/radar-project-pipeline:v0.3.2 .
docker run --rm --read-only --tmpfs /tmp \
  --mount "type=bind,src=$PWD/tests,dst=/tests,readonly" \
  --entrypoint python ghcr.io/beolle/radar-project-pipeline:v0.3.2 \
  -m unittest discover -s /tests -p test_cloudflare.py -v
docker push ghcr.io/beolle/radar-project-pipeline:v0.3.2
```

Argo CD의 `radar` Application을 수동 Sync하여 `k8s/airflow-rbac.json`의 Role/RoleBinding을 반영합니다. 권한은 `radar` namespace의 Pod 생성·조회·로그·정리와 이벤트 조회뿐입니다. Secret 조회, 다른 namespace 권한, ClusterRole은 추가하지 않습니다.

```bash
kubectl auth can-i create pods -n radar --as=system:serviceaccount:pipeline:airflow-worker
kubectl auth can-i get pods/log -n radar --as=system:serviceaccount:pipeline:airflow-worker
kubectl -n pipeline exec deployment/airflow-scheduler -c scheduler -- \
  airflow pools set radar_collection 1 'Serialize Radar collection pods'
```

#### DAG 배포와 첫 실행

새 이미지의 빌드·검사·GHCR push가 성공한 다음에만 `airflow/radar_collection.py` 한 파일을 기존 **airflow-practice 저장소의 git-sync가 읽는 경로**에 추가·커밋·push합니다. Radar 저장소만 push해도 기존 Airflow가 이 파일을 읽는 것은 아닙니다. 기존 git-sync URL을 Radar 저장소로 바꾸지 않습니다. 실제 설치된 Airflow/Kubernetes provider 버전의 import 검사를 통과해야 합니다.

```bash
kubectl -n pipeline exec deployment/airflow-scheduler -c scheduler -- \
  airflow dags list-import-errors
kubectl -n pipeline exec deployment/airflow-scheduler -c scheduler -- \
  airflow dags list
```

UI에서 `radar_daily`, `radar_weekly`와 pool을 확인한 뒤 최초 실행도 Airflow에서 진행합니다. 수집 로그, DB 주차·지역별 행 수, raw 파일, 공개 화면을 확인하고 스케줄을 활성화합니다. API 버전 호환·이미지 다운로드·실제 국가별 가용성·100만 건 PostgreSQL 적재는 이 서버 검증에서 확인할 사항입니다.

롤백은 두 Radar DAG를 pause하고 실행 중 task/수집 Pod 종료 여부를 확인한 뒤 DAG 파일만 이전 버전으로 되돌립니다. 기존 v0.2.0 이미지·raw·DB는 삭제하지 않습니다. 새 주간 자료는 12개 bucket이므로 이전 코드로 **재적재하지 말고**, 기존 웹으로 조회만 유지합니다. 스키마 변경은 없습니다. 주간 옛 4개/새 12개 방식의 이력을 섞어 비교하면 세분화 자체가 상승으로 보일 수 있으므로 기존 주간 이력이 있는 DB에는 별도 전환 처리가 필요합니다. 현재 확인된 운영 데이터는 일간 WORLD/KR뿐입니다.

## 개인 서버 배포 전 남은 작업

1. Gemini v0.4.0 빌드·검사·push 후 태깅 DAG 배포, 100개 실제 API/DB 검증
2. Gemini 상세 URL Context adapter·초기 모집단 관리·별도 도메인 상세정보
3. 첫 실행 후 스케줄 활성화, 장애 알림, DB 백업과 복구 확인
4. 실제 수집 데이터의 외부 화면 확인, 장비 기준 부하 검사
5. 사후 수정·과거 백필 시 영향받는 mart 재계산, 필요하면 별도 migration 체계

로컬 npm dev/start는 localhost에만 바인딩하고 컨테이너에서는 Pod 네트워크를 위해 0.0.0.0으로 실행합니다. 공개 접속은 전용 Cloudflare Tunnel의 HTTPS 주소를 사용합니다. API 키·DB 비밀번호를 `NEXT_PUBLIC_*` 변수나 Git에 넣지 않습니다. `.env`와 data, node_modules, 가상환경은 `.gitignore`로 제외되고 Docker 빌드에서도 비밀정보를 제외합니다. Kubernetes 내부 웹과 공개 HTTPS의 HTTP 응답까지 확인했으며 실제 데이터 화면 검증은 별도입니다.

## 출처

- [Cloudflare Radar 순위 데이터](https://developers.cloudflare.com/radar/investigate/domain-ranking-datasets/)
- [Cloudflare 순위 API](https://developers.cloudflare.com/api/resources/radar/subresources/ranking/methods/top/)
- [Cloudflare dataset 목록](https://developers.cloudflare.com/api/resources/radar/subresources/datasets/methods/list/)
- [Cloudflare dataset CSV 다운로드](https://developers.cloudflare.com/api/resources/radar/subresources/datasets/methods/get/)
- [Airflow KubernetesPodOperator](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/operators.html)
- [Cloudflare Radar API 토큰 준비](https://developers.cloudflare.com/radar/get-started/first-request/)
- [Gemini URL Context](https://ai.google.dev/gemini-api/docs/url-context)
- [Gemini 사용량 한도](https://ai.google.dev/gemini-api/docs/rate-limits)
- [Kubernetes Job](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
- [Kubernetes Secret](https://kubernetes.io/docs/concepts/configuration/secret/)
- [Kubernetes local 볼륨](https://kubernetes.io/docs/concepts/storage/volumes/#local)
- [Kubernetes PV 예약과 보존](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
- [uv Docker 연동](https://docs.astral.sh/uv/guides/integration/docker/)
- [Argo CD Application 명세](https://argo-cd.readthedocs.io/en/stable/user-guide/application-specification/)
- [Argo CD 자동 동기화 정책](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/)
- [Argo CD 리소스별 동기화·삭제 옵션](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/)
- [Cloudflare Tunnel Kubernetes 배포](https://developers.cloudflare.com/tunnel/guides/kubernetes/)
- [cloudflared 릴리스](https://github.com/cloudflare/cloudflared/releases)

Cloudflare 데이터를 공개할 때는 해당 데이터 이용 조건과 출처 표시를 유지해야 합니다. 저장소에는 제3자 원본 데이터나 실제 API 응답을 포함하지 않습니다.

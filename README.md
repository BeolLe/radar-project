# Radar Project

Cloudflare Radar 도메인 순위 이력을 누적하고, 변화 신호와 Gemini 태그를 조회하는 **개인 서버용 프로젝트 초안**입니다.

**현재 상태:** 파일 기반 적재·변화 계산·태그 요청 준비/결과 검증·Next.js 조회를 구현했습니다. 실제 Cloudflare/Gemini API 수집기와 자동 운영 worker는 아직 연결하지 않았습니다. `.example` 가상 데이터로 구조를 확인하는 단계이며 운영 완성품이나 API 수집 실적이 아닙니다.

## 구성

- Python 3.12+ / uv: 입력 검증, raw Parquet 저장, PostgreSQL 적재
- PostgreSQL: `stage` → `core` → `mart`
- Next.js / React: 서버 측 DB 조회, 날짜·국가·태그 필터, 도메인 이력
- Gemini `gemini-3.1-flash-lite`: 100개 잠정 분류 → 20 URL 상세 점검의 **요청·결과 계약**
- 초기 태그 44개: 분야 22개, 용도 16개, 도메인 역할 6개

```text
완료된 수집 자료(JSON 계약) → raw Parquet → stage 1차 적재
                                      → core/mart 2차 적재 → Next.js 서버 → 브라우저
                                             └→ 태깅 요청 준비 → [외부 worker 미연결]
                                                               → 결과 검증·적재 → 화면
```

raw 파일은 Git에서 제외합니다. PostgreSQL을 브라우저에 직접 노출하지 않습니다. 기존 다른 프로젝트의 DB·수집 작업은 사용하거나 변경하지 않습니다.

배포 대상은 **Ubuntu 24.04의 Kubernetes, namespace `radar`**이며 웹·파이프라인 모두 Pod에서 실행합니다. 기존 PostgreSQL을 사용합니다. Git 저장소는 [BeolLe/radar-project](https://github.com/BeolLe/radar-project)입니다. GHCR에 이미지를 보관하고 **Argo CD 수동 Sync**로 웹을 배포합니다. 서버 배포는 아래 Kubernetes 절을 따르고, Docker Compose는 로컬 개발용으로만 사용합니다.

## 파일

```text
radar/                  Python CLI, 입력 계약, 태깅 검증
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

이 초안의 `ingest`는 Cloudflare API 원본 응답을 바로 받지 않습니다. 향후 수집기가 응답과 완료 메타데이터를 아래 계약으로 변환해야 합니다. API의 `description` 주석은 사이트 소개문이 아닙니다.

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

주간 입력은 `kind=weekly`, `location=WORLD`이며 `sources`에 bucket `100000`, `200000`, `500000`, `1000000` 네 개가 있어야 합니다. 각 source의 rows에는 domain이 들어갑니다. 각 파일의 도메인 중복은 거부하고, 파일 간 누적 포함 관계를 검사한 뒤 가장 작은 bucket으로 합칩니다.

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

### 요청 준비 — API 호출 없음

```sh
uv run --env-file .env python -m radar prepare-tags --phase preliminary --limit 100 > preliminary-request.json
uv run --env-file .env python -m radar prepare-tags --phase detail --limit 20 > detail-request.json
```

요청에는 도메인 ID·이름·URL·44개 허용 코드·프롬프트 규칙이 들어갑니다. 도메인별 1차 완료와 상세 점검 완료를 별도로 취급하므로 1차 태그가 있어도 detail 대상입니다.

**이는 offline 요청 초안이지 동작하는 worker가 아닙니다.** 대상 선점·일일 200요청 예산·초기 모집단 고정·1차 종료 후 2차 전환은 아직 구현하지 않았습니다. 결과 적재 전 같은 명령을 반복하면 같은 대상이 나올 수 있습니다. unknown/실패 이력은 자동 무한 재시도하지 않으며 재점검 정책도 후속 구현입니다.

### 결과 검증·적재

향후 Gemini adapter는 SDK 응답을 다음 내부 형식으로 변환해야 합니다. `tool_evidence`는 **모델이 쓴 JSON이 아니라 URL Context 도구 메타데이터에서** 추출해야 합니다. 이 파일 입력만으로 웹을 실제 조회했다고 독립 검증할 수는 없습니다.

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

로컬에서 단위·배포 계약 검사 12개(Parquet 왕복·namespace·정적 PV 연결·Argo CD 수동 정책 포함)와 `kubectl kustomize k8s` 렌더링을 확인합니다. Next.js 타입 검사·프로덕션 빌드는 기존 초안에서 통과했습니다.

사용자 제공 Ubuntu 실행 로그에서 **컨테이너 이미지 2개 빌드, 단위 검사 8개, `radar_test` DB 통합 검사 1개, 조회 전용 계정으로 웹 `/api/ready`의 `ready=true`**, GHCR 이미지 2개 `v0.1.0` 업로드 성공 및 raw PV/PVC의 `Bound` 상태를 확인했습니다. 이 결과는 실제 Kubernetes 이미지 다운로드·볼륨 파일 쓰기·순위/이력 화면 전체 검증·대량 적재 성능 검증을 의미하지 않습니다. 실제 Cloudflare·Gemini API 호출은 아직 확인하지 않았습니다.

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

**이 절의 명령은 서버에서 사용자가 실행할 절차입니다. 코드 업로드만으로 배포되지는 않습니다.** Application은 기존 Argo CD의 `argocd` namespace에 등록하고 앱 리소스는 `radar` namespace에 둡니다. 자동 동기화·자동 삭제·self-heal·이미지 자동 갱신은 설정하지 않았습니다. GitHub Actions와 실제 수집 CronJob도 아직 없습니다. JSON은 Kubernetes가 기본 지원하는 manifest 형식입니다.

**첫 Sync 전 준비:** GHCR 이미지와 pull 권한, DB·조회 권한, `radar` namespace의 Secret, 아래 raw 디렉터리 준비가 모두 필요합니다. StorageClass 없이 `ronny` 노드의 `/mnt/data/ronny-project/radar-data`를 사용하는 정적 local PV로 설정했습니다. 실제 서버의 PV/PVC 연결과 Pod 파일 쓰기는 아직 검증하지 않았습니다. 예전 `radar-project` namespace에 리소스를 이미 배포했다면 이름 변경은 데이터 이전이 아니므로 별도 이전 계획 없이 기존 namespace/PVC를 삭제하지 않습니다.

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

port-forward를 실행한 컴퓨터의 `http://127.0.0.1:3000`에서 확인합니다. 원격 Ubuntu에서 실행했다면 Mac/모바일의 localhost가 아닙니다. 원격 확인에는 SSH 포워딩이나 기존 접속 경로가 추가로 필요합니다. 이 초안은 ClusterIP까지만 제공하며 Ingress·도메인·HTTPS·외부 공개 경로는 아직 만들지 않았습니다.

`/api/ready`는 DB·조회 테이블 접근 실패 시 503을 반환합니다. DB 장애는 readiness만 실패시키며 liveness는 웹 프로세스 응답 여부를 따로 확인합니다. 아직 자료가 없는 빈 DB도 스키마와 권한이 정상이면 ready입니다.

### 6. 샘플 검증과 파이프라인 실행

샘플은 운영 DB가 아니라 별도 개발 DB에서 확인합니다. `pipeline-job.json`을 Git 제외 경로인 `k8s/local/`에 복사해 Secret 참조를 개발 DB 것으로 바꾸고 `args`를 `["demo"]`로 바꾸면 같은 Job 실행 절차로 샘플 적재를 검증할 수 있습니다. 실제 파일 적재는 PVC에 입력 파일을 준비한 뒤 `["ingest", "/data/input/snapshot.json"]`을 사용합니다. Job 입력 파일 배달과 실제 API 수집기는 아직 자동화하지 않았습니다.

raw는 `/data/raw`에 기록되어 Pod가 종료돼도 PVC에 남습니다. 웹에는 raw PVC를 연결하지 않습니다. 자원 설정(웹 512Mi, 파이프라인 4Gi 제한)과 Job 제한 시간 30분은 초안의 시작값이지 100만 도메인 처리 실측값이 아닙니다. 입력 전체를 메모리에 올리는 현재 구현은 전수 수집 전에 메모리·시간 실측이 필요합니다. 초기 Job은 하나씩 실행하며 실제 수집·태깅 worker가 완성된 후 중복 실행 방지와 공통 API 예산을 포함해 CronJob을 연결합니다.

## 개인 서버 배포 전 남은 작업

1. 공식 API 표본에 맞춘 Cloudflare 수집 adapter 및 원본 기간·파일 완결성 검증
2. Gemini 실제 호출 adapter, 공통 일일 예산·재시도·작업 선점·초기 모집단 관리
3. 지속 실행 스케줄, 장애 알림, DB 백업과 복구 확인
4. 전용 DB 읽기 계정 실제 설정, HTTPS·공개 경로·접근 정책, 장비 기준 부하 검사
5. 사후 수정·과거 백필 시 영향받는 mart 재계산, 필요하면 별도 migration 체계

로컬 npm dev/start는 localhost에만 바인딩하고 컨테이너에서는 Pod 네트워크를 위해 0.0.0.0으로 실행합니다. 외부 공개 시에는 HTTPS reverse proxy/Ingress 뒤에서 실행하세요. API 키·DB 비밀번호를 `NEXT_PUBLIC_*` 변수나 Git에 넣지 않습니다. `.env`와 data, node_modules, 가상환경은 `.gitignore`로 제외되고 Docker 빌드에서도 비밀정보를 제외합니다. Ubuntu의 일시적인 Docker 테스트 실행과 Kubernetes 운영 배포는 별개이며, 후자는 아직 확인하지 않았습니다.

## 출처

- [Cloudflare Radar 순위 데이터](https://developers.cloudflare.com/radar/investigate/domain-ranking-datasets/)
- [Cloudflare 순위 API](https://developers.cloudflare.com/api/resources/radar/subresources/ranking/methods/top/)
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

Cloudflare 데이터를 공개할 때는 해당 데이터 이용 조건과 출처 표시를 유지해야 합니다. 저장소에는 제3자 원본 데이터나 실제 API 응답을 포함하지 않습니다.

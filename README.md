# 한국 정부 혜택 Hybrid RAG — 초기 개발 기반

**개인 맞춤형 정부 지원제도 탐색 및 신청 안내**를 위한 개발 기반이다. 제품 대상은 취업·구직, 이사·주거, 결혼, 출산·양육 등 생활 변화를 겪는 개인이며 청년으로 제한하지 않는다. 초기 coverage는 실제 확보·검증한 프로그램만, 초기 검증 cohort는 청년 구직자·신혼부부·영유아 양육 가구로 제안한다. 기업 지원은 초기 범위 밖이다.

현재는 **보조금24 합성 fixture 수집·정규화·적재·raw 재생, 오프라인 프로파일링, 평가 데이터 계약 검증**을 구현했다. 실제 정책 데이터·live 인증은 미검증이며 검색·생성·API/UI는 아직 없다. 충분한 근거가 있으면 약 3–5개·최대 5개를 안내하는 것이 제품 방향이다. 부족하면 더 적게 반환하며 결과를 채우거나 전체 혜택 목록·공식 자격 결정으로 표현하지 않는다. RAG/Hybrid의 품질 이점은 비교 실험할 가설이다.

현재 검증 환경은 Windows 11 / PowerShell / Python 3.12.9 / uv 0.12.10이다. 기존 브랜치 `seok-kim`에서 작업했다. Python 3.11/3.12를 지원 대상으로 선언했으며 3.11·Linux 실행은 미검증이다.

## 설치와 오프라인 실행

저장소 루트에서 실행한다. Python 3.12를 명시하며 `py`의 기본 3.14를 사용하지 않는다. `.env`가 이미 있다면 덮어쓰지 않는다. 처음 오프라인 실행에는 `.env`가 필요 없다.

```powershell
uv sync --locked --python 3.12
New-Item -ItemType Directory -Force data | Out-Null
$env:DATABASE_URL = 'sqlite:///data/benefits.db'
uv run --locked alembic upgrade head
uv run --locked benefit-rag fixture
uv run --locked benefit-rag fixture
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

첫 적재는 inserted 3, 두 번째는 unchanged 3을 기대한다. 이는 합성 자료 실행이다. 원문은 SQLite DB의 `raw_response`에 저장되며 API 호출이 없다. `fixture`는 2건/페이지·최대 2페이지가 기본이다. 모든 페이지를 확보하지 못하면 exit 2 및 partial/failed가 된다. 완료는 해당 수집 범위에 한정된 완료다.

`run_id`는 CLI 결과에 표시된다. 다음 명령의 자리에 실제 ID를 넣어 raw만 재생한다.

```powershell
uv run --locked benefit-rag replay --run-id <run_id>
```

환경변수가 `.env`보다 우선한다. PostgreSQL로 전환할 때 앞서 설정한 `DATABASE_URL`을 바꾸거나 `Remove-Item Env:DATABASE_URL`로 해제한다. uv cache 권한이 제한되는 환경에서 이미 설치를 마쳤다면 `.venv/Scripts/python.exe -m pytest`, `.venv/Scripts/alembic.exe upgrade head`, `.venv/Scripts/benefit-rag.exe fixture`로 실행할 수 있다. Windows sandbox에서 임시 디렉터리 또는 Docker pipe 권한이 거부되면 필요한 실행 권한을 부여한다. 승인 없이 우회하지 않는다.

Linux/macOS에서는 `mkdir -p data`, `export DATABASE_URL=sqlite:///data/benefits.db`로 환경을 설정하고 동일 uv 명령을 사용한다. 직접 실행 경로는 `.venv/bin/`이다. Linux 절차는 아직 실행 검증하지 않았다.

## 저장 데이터 프로파일과 평가 계약

위 fixture/replay 결과의 실제 run_id를 사용한다. 두 명령은 네트워크·유료 호출·모델 다운로드 없이 실행된다.

```powershell
uv run --locked benefit-rag profile --run-id <run_id> --dataset-kind fixture --output data/profile.json
uv run --locked benefit-rag validate-evaluation --file tests/fixtures/evaluation/synthetic.json
```

profile은 종료된 단일 run의 raw를 읽고 상태·관측 수·명시적 분모의 결측·문자 길이·분야/대상 관측값·URL presence/저장 상태·raw manifest를 출력한다. fixture/live namespace 불일치는 거부하며 과거 통계를 최신 프로그램 값으로 바꾸지 않는다. 데이터 0건이면 분모 0·길이 통계 null이다. 미완료 표본과 합성 통계는 전체 corpus로 일반화할 수 없다. URL 상태 미저장은 not_stored_for_observation으로 표시한다. 상세 계약은 [data_sources.md](docs/data_sources.md)에 있다.

평가 예제는 명시된 **합성 계약 테스트**이며 실제 혜택 라벨·성능 측정이 아니다. validation은 구조·참조와 합성 fixture의 SHA/원문 span만 검사한다. policy_truth_verified=false이며 검증 통과로 정답을 인증하지 않는다. 네 baseline(SQL/BM25/vector/hybrid), filtering ablation, 제안 pilot 60문항과 지표 분모는 [evaluation.md](docs/evaluation.md)에 있다. [planning_alignment.md](docs/planning_alignment.md)는 DOCX 정렬·외부 주장 상태를 기록한다.

## PostgreSQL / pgvector 실행

Docker Desktop Linux engine을 사용한다. 이미지 `pgvector/pgvector:0.8.0-pg16`, host bind `127.0.0.1:55432`, 영속 named volume을 구성했다. 예제 비밀번호는 로컬 개발 전용 공개 값이다. 실제 환경에서는 `.env.example`을 참고해 별도 비밀 값을 설정한다.

```powershell
docker compose up -d --wait db
$env:DATABASE_URL = 'postgresql+psycopg://benefits:local-dev-only@127.0.0.1:55432/benefits'
uv run --locked alembic upgrade head
uv run --locked benefit-rag fixture
uv run --locked alembic check
$env:TEST_DATABASE_URL = $env:DATABASE_URL
uv run --locked pytest -m postgres
docker compose stop db
```

PostgreSQL 테스트는 지정 DB 안에 무작위 `test_benefits_` 스키마를 만들고 자기 스키마만 정리한다. 운영 DB를 지정하지 않는다. vector extension 생성 권한이 필요하며 extension은 downgrade/테스트 후에도 남긴다. 운영에서는 관리자가 extension을 준비하고 역할 권한을 별도로 설계한다. 위 stop은 컨테이너 데이터 volume을 보존한다. 초기에는 embedding/vector 컬럼이나 검색 인덱스를 만들지 않는다.

## live 표본 검증

[공식 보조금24 데이터셋](https://www.data.go.kr/data/15113968/openapi.do)에 활용신청하고 승인된 Decoding 키를 `GOV24_SERVICE_KEY`로 `.env`에 설정한다. 키를 명령행·화면·Git에 노출하지 않는다. 현재 실행 환경에는 인증키가 없으며 실제 live 성공 기록이 없다.

```powershell
uv run --locked benefit-rag live --page-size 3 --max-pages 1
```

이 명령은 3건 표본만 요청하므로 데이터가 더 있으면 partial/exit 2가 정상이다. 기본값을 전수 수집 승인으로 해석하지 않는다. 인증 실패도 최종 HTTP raw와 오류 코드로 기록한다. 실제 응답을 받은 뒤 [조사 문서](docs/data_sources.md)의 체크리스트로 타입/코드/결측/지역/신청 정보/URL을 검증한다. `.env.example`의 OpenAI 항목은 후속 생성 단계용이며 현재 코드에서 사용하지 않는다.

## 구조와 제한

- [AGENTS.md](AGENTS.md): 범위·개발/검증·비밀·출처·모델 배분 규칙.
- [architecture.md](docs/architecture.md): 흐름, 잠정 스키마, 모듈 계약, 삼값 자격, 후속 검색/생성 설계.
- [data_sources.md](docs/data_sources.md), [source_specs.json](docs/source_specs.json): 공식 문서와 접근 상태, 명세 근거.
- `src/benefit_rag/`: HTTP/fixture adapter, parser, loader, raw replay, DB models, CLI, 최소 연령/AND/OR evaluator.
- `migrations/`: Alembic frozen 초기 schema. `tests/fixtures/gov24/`: 실제 정책이 아닌 합성 3건.
- [verification.md](docs/verification.md): 실제 검사 결과와 미확인 항목.
- [next_tasks.md](docs/next_tasks.md), [evaluation.md](docs/evaluation.md): 구체적 인계와 후속 평가 계획.
- [planning_alignment.md](docs/planning_alignment.md): 읽은 기획서, 방향·가설·외부 주장 검증 대장.

canonical schema는 잠정이다. 수집 텍스트의 자격은 unresolved, 신청 상태는 unknown으로 둔다. 검증된 규칙/사용자 확인 정보만 삼값으로 평가한다. 소득·지역·기간 evaluator는 미구현이다. URL은 원문 값만 보존하고 공식성/접근성 검증 전 unverified다. 합성 fixture에 URL은 넣지 않았다.

한 source/dataset_kind에 단일 writer만 사용한다. 중간 실패 시 이미 적재한 페이지는 남기고 raw를 재생할 수 있다. 강제 종료 run 자동 복구, 동시 수집 잠금, 전체 계정 rate budget, 상세/조건 API, 실제 정책 범위 선별, 구조화 규칙 추출은 후속 작업이다. 누락 record를 자동 삭제하거나 종료시키지 않는다. 현재 데이터로 추천 결과를 제공하지 않는다.

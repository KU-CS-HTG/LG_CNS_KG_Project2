# 실행 검증 및 인계 기록

기준일: **2026-10-16, Asia/Seoul**. 이 기록은 실제 실행 결과다. 검색·생성 품질은 측정하지 않았다.

아래 ‘초기 기반’은 이전 실행의 역사적 기록이다. 이번 정렬 revision의 시작 커밋·명령·결과는 문서 끝의 **기획 정렬 revision 검증**에 별도로 기록하며 이전 결과를 현재 통과 근거로 사용하지 않는다.

## 초기 기반 검증 (이전 실행 기록)

## 저장소와 환경

시작 시 cwd는 `C:\Users\Admin\Desktop\LG_CNS_KG\LG_CNS_KG_Project2`, branch는 `seok-kim`, HEAD는 `d417ed8 chore: initialize seok-kim branch`였다. Git은 이미 초기화되어 있었고 `.git` 외 파일과 uncommitted 변경은 없었다. 작업 디렉터리 및 루트까지의 상위 경로에서 적용할 AGENTS.md는 발견되지 않아 루트에 새로 작성했다. 브랜치 변경·git init·커밋·push는 하지 않았다.

| 항목 | 관측 |
|---|---|
| OS/shell | Windows 11 Pro, build 26200 / PowerShell |
| Python | `python` 3.12.9, 별도 3.14 설치 및 `py` 기본은 3.14. 프로젝트는 3.12 지정 |
| 패키지 관리 | uv 0.12.10, pip 24.3.1 관측. 프로젝트 설치/lock은 uv로 통일 |
| CPU | Intel64 Family 6 Model 170 Stepping 4, logical CPUs 22. 정확한 제품명/physical core 수는 본 기록에서 확정하지 않음 |
| RAM | 총 31.49GiB, 조사 시 가용 6.33GiB. 순간 가용량이며 inference capacity 보장이 아님 |
| GPU | NVIDIA GeForce RTX 4070 Laptop GPU, 8188MiB, driver 566.14 (`nvidia-smi`) |
| Docker | Desktop 4.92.0, engine 29.8.0, Linux amd64, `desktop-linux` context |
| 실제 개발 DB | PostgreSQL 16.10 (Debian), pgvector 0.8.0, Compose healthcheck healthy |

CIM·Docker pipe·공식 문서 HTTPS·uv cache·pytest temp 접근은 sandbox에서 일부 거부되었다. 필요한 명령은 승인 요청 후 실행했다. CPU/memory는 우선 Python OS/Win32 조회로 관측했고 GPU는 nvidia-smi로 확인했다. CUDA toolkit, PyTorch, BGE-M3 실행 호환성은 검사하지 않았다.

`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `DATA_GO_KR_SERVICE_KEY`, `GOV24_SERVICE_KEY`, `BOKJIRO_SERVICE_KEY`, `DATABASE_URL`, `HF_TOKEN`은 조사 시 프로세스에 모두 미설정이었다. 값은 출력하지 않았다. 이후 DB 검증 명령에만 공개 로컬 예제 연결 값을 프로세스 환경으로 설정했다. 비밀 파일 내용은 보고 목적으로 읽지 않았다.

## 의존성

`uv sync --python 3.12` 설치 성공 후 `uv sync --locked --python 3.12` 재현 검증 성공: 29 packages resolved/checked, 추가 모델 다운로드 없음.

핵심 실제 설치 버전은 SQLAlchemy 2.0.54, psycopg/psycopg-binary 3.3.6, Alembic 1.20.0, Pydantic 2.14.1, pydantic-settings 2.16.0, HTTPX 0.28.1, tzdata 2026.5, pytest 8.4.2, Ruff 0.17.1이다. 전체 exact dependency/artifact는 `uv.lock`에 있다. FastAPI/OpenAI SDK/Kiwi/rank_bm25/BGE-M3/Streamlit은 후속 기술이며 설치·호환성을 현재 검증했다고 주장하지 않는다.

## 검사 결과

| 검사 | 실제 결과 |
|---|---|
| 기본 pytest | **48 passed, 1 deselected**, 1.26초. 외부 API·유료 호출·모델 다운로드 없음 |
| PostgreSQL suite | **1 passed, 48 deselected**, 격리 수정 후 기존 public DB가 있는 상태에서 연속 두 번 성공 (0.60초/0.63초) |
| Ruff check | `All checks passed!` |
| Ruff format check | 통과 |
| compileall | `python -m compileall -q src migrations` exit 0 |
| SQLite migration/check | upgrade head 성공, `No new upgrade operations detected.` |
| PostgreSQL migration/check | upgrade head 성공, `No new upgrade operations detected.` |
| PostgreSQL downgrade→upgrade | 전용 테스트 스키마에서 성공. JSONB/native raw byte 저장 및 vector extension 확인 |
| live 키 누락 경로 | 실제 CLI subprocess exit **2**, `credential_missing` 확인, 정책 데이터 HTTP 호출 없음 |
| 문서/fixture 검사 | 로컬 문서 링크 누락 0, 검사한 소스/문서의 trailing whitespace 0, 공식 명세 snapshot 및 fixture JSON 파싱 성공 |

기본 suite는 재적재 중복 방지, 내용 변경 revision, 조회수/원본 수정일시 변화의 비버전 처리, raw 재생 provenance, 페이지 제한/중간 실패/총건수 변동/중간 빈 페이지/페이지 불일치, 필수값 누락·잘못된 타입·중복, optional 타입·URL 문제, 실패 body 보존, DB 페이지 rollback과 카운터, 정상 빈 수집, 인증키 요청 metadata 비저장, timeout/retry/Retry-After/redirect, 연령 경계와 확인 여부, 삼값 AND/OR 전체 truth table을 검증한다.

처음 기본 pytest는 30개 통과 후 Windows temp 접근 거부로 DB fixture 15개가 시작하지 못했다. 승인 후 해결했고 Alembic 설정의 deprecation 경고도 수정했다. 생성된 캐시 디렉터리 두 곳 때문에 Ruff가 한 번 실패/종료되어 해당 캐시만 경로 확인 후 정리했다. 이후 전체 정적 검사가 통과했다.

PostgreSQL 최초 테스트는 빈 DB에서 통과했지만 개발 DB에 테이블이 생긴 후 `search_path`의 public fallback이 기존 `alembic_version`을 발견해 테스트 격리를 깨는 결함을 드러냈다. 이를 **무작위 전용 스키마만 사용하는 search_path**로 수정했고 current_schema·해당 스키마 내 version table·적재 전 0건 검증을 추가했다. 최종 연속 실행은 모두 통과했다. 이 반복은 격리 결함 해결을 확인하기 위해 수행했다. 테스트는 자기 스키마만 삭제하며 기존 public 테이블과 영속 volume을 삭제하지 않는다.

## 실제 CLI smoke

| 저장소 | 실행 | 관측 |
|---|---|---|
| PostgreSQL | fixture 첫 실행 | completed, inserted 3, 2페이지, rejected 0 |
| PostgreSQL | fixture 두 번째 | completed, unchanged 3, 새 program/version/document 없음 |
| PostgreSQL | raw replay (함수 및 CLI) | completed, unchanged 3, fixture kind 유지, API 미사용 |
| SQLite | fixture 첫/두 번째 | 각각 inserted 3 / unchanged 3, completed |

PostgreSQL smoke의 3회 실행 직후 프로그램 3개·버전 3개·문서 12개를 확인했다. raw/run은 매 실행마다 추가 관측을 남기는 것이 의도된 동작이다. 합성 입력과 실제 정책을 혼동하지 않는다. 대표 첫/두 번째 PostgreSQL run은 `cd6466e8-c4a4-4584-a73e-bbb78611b9f1`, `dc3c6f25-139e-400a-9562-76c9262ca437`; SQLite는 `254a708a-a5c6-4bce-8239-53f35b3b8274`, `1e0d5f43-e24c-486c-b20f-da498fac0997`이다. 로컬 비공개 DB/volume에만 있으며 Git 대상이 아니다.

검증 후 `docker compose stop db`로 개발 DB 컨테이너를 중지했다. 영속 volume과 합성 자료는 보존했으며 README의 `docker compose up -d --wait db`로 다시 실행할 수 있다. 최종 브랜치는 `seok-kim`이고 신규 파일들은 커밋하지 않은 상태다.

## 접근 상태와 남은 검증

공식 포털 문서 세 건과 연결 Swagger를 확인했다. **실제 정책 응답 0건**, 실응답과 명세의 차이·결측률·중복률·지리 분포·대상 범위는 미확인이다. 구체적인 소스/endpoint/파라미터/한도는 [data_sources.md](data_sources.md)를 참고한다. canonical schema는 provisional이며 키 승인과 소량 live 표본 후 재검토한다.

live HTTP 어댑터는 실제 endpoint에 대한 인증 성공을 입증하지 않았고 기본 suite의 HTTP 검사는 MockTransport다. 상세/지원조건 API, 복지로 XML 파서, 한국어 tokenizer, embedding 추론·메모리/latency, 검색 index·RRF, 생성 endpoint/모델 권한, 소득·지역·기간 evaluator, API/UI는 미구현/미검증이다. SQLite와 Windows Python 3.12 외 지원 환경은 실제 검사하지 않았다. 품질 수치나 향상 주장은 없다.

인계의 첫 작업은 승인 키 공급 후 보조금24 표본 검증(Sol 6.1 High)이다. 이어 상세/조건 계약, 프로필/조건 evaluator, corpus 평가·검색으로 진행한다. 정해진 CI 명령 구현만 Luna 6에 배정할 수 있다. Astra는 근거가 있는 설계 충돌이 생겼을 때 정확한 질문과 함께 요청한다. 각 작업의 파일·입출력·선행 조건·합격 기준은 [next_tasks.md](next_tasks.md)에 명시했다.

## 기획 정렬 revision 검증 (이번 실행)

2026-10-16 Asia/Seoul. 시작 branch **seok-kim**, commit **c17cd68deb6ce3d9df642e3bb793ff321a3957fd**이며 작업 트리는 clean이었다. 이전에 알려진 HEAD와 같았다. 적용 AGENTS 및 상위 경로를 재확인하고 기존 코드·문서·명세를 다시 읽었다. 지정 DOCX 두 사본의 본문을 읽어 작성자 칸 외 동일함을 확인했고 원본은 수정하지 않았다. 자료 위치·hash·기획/외부 주장 검증은 [planning_alignment.md](planning_alignment.md)에 있다.

### 실제 변경 범위

기존 수집·적재·재생·삼값 함수는 그대로 유지했다. 추가한 것은 profiling 모듈/CLI, evaluation_contract 모듈/CLI와 합성 계약 예제·집중 테스트, 기존 PostgreSQL 테스트의 profile 검사다. 제품/coverage/cohort 구분, 결과 수 계약, 상세 덮어쓰기 방지 설계, 기술 결정표, 네 baseline·filter ablation·평가 데이터/지표 분모·인계 문서를 정렬했다. **의존성·uv.lock·DB schema·적용된 초기 migration·.gitignore는 변경하지 않았다.**

### 이번에 실행한 명령과 결과

| 명령/검사 | 실제 결과 |
|---|---|
| `uv sync --locked --python 3.12` | 성공, 29 packages checked. 의존성 변경 없음 |
| `uv run --locked pytest --tb=short` | **82 passed, 1 deselected**, 1.78초. 기존 48개와 신규 34개 검증 |
| `uv run --locked pytest -m postgres --tb=short` | **1 passed, 82 deselected**, 0.78초. 로컬 개발 DB의 무작위 전용 스키마에서 수행 |
| `uv run --locked ruff check .` | All checks passed |
| `uv run --locked ruff format --check .` | 통과 |
| SQLite 새 smoke DB migration/check | upgrade 성공, No new upgrade operations detected |
| CLI fixture 첫/두 번째 및 replay | inserted=3 / unchanged=3 / replay unchanged=3 |
| CLI profile 출력·반복 | object=3, target missing=1 / denominator=3, 재적재·재생 이후에도 첫 run profile 완전 일치 |
| CLI validate-evaluation | synthetic_contract, case_count=2, structural_validation_passed, evidence_storage_checked=true, **policy_truth_verified=false** |
| 공식 Swagger 문서 비교 | 문서 다운로드 성공, 저장 명세와 paths/definitions 동일. 정책 API 요청 아님 |
| 최종 정적/문서 검사 | compileall 성공, Ruff 재확인 통과, Git diff whitespace 검사 통과, 깨진 로컬 링크·검사 파일 trailing whitespace 0 |

uv cache·pytest temp·Docker pipe·공개 문서 HTTPS는 제한 환경에서 거부되어 필요한 명령을 escalation 후 실행했다. 자동 승인 검토의 거부는 없었다. 초기 신규 테스트가 pytest entrypoint에서 tests 패키지 import에 의존하는 문제를 발견하여 테스트 내부 adapter로 분리했고 최종 기본 suite가 통과했다. 초기 실패를 통과 결과로 기록하지 않는다.

신규 검사는 namespace 혼합 거부, 과거 raw 통계 불변, 결측 분모·비문자열 구분·빈/실패 표본·읽기 전용, 합성→실제 라벨 오표기 방지, 필수 계약·버전/근거 참조·span·SHA, needs_confirmation 보존, split 누출 방지, 소득 비율만 있을 때 금액 미발명, profile의 HTTP 미생성·평가 검증의 DB 미접속·오류 입력값 비출력을 포함한다. 실제 정책 정답이나 검색 성능을 검사한 것이 아니다.

### CLI smoke 재현 근거

새 로컬 파일 `data/alignment-smoke-20261016.db`를 사용했으며 기존 DB를 초기화하지 않았다. 첫 run `637b32cf-f56f-4a17-b9e0-2be455eacbeb`. `data/alignment-profile.json`의 snapshot hash는 `05c34a46cf26bd694234abdd106323df3537ca8d55602ddef890b308e1c36bc0`이다. 보고서는 합성 raw 3건·2페이지 범위이며 전체 정책 corpus 수가 아니다. 원문·통계는 Git 제외 로컬 data에 보관한다.

### live 상태·미확인·후속 의존성

현재 GOV24/DATA_GO_KR/BOKJIRO/OPENAI 키 환경변수는 미설정이고 `.env`도 없었다. 키 값·인증 URL을 출력하지 않았다. 이번 **정책 live API 호출 0회**, 실제 정책 라벨 0개, 유료 생성 호출·대형 모델 다운로드 0회다. 공개 포털의 공시 정보와 공식 Swagger만 다시 확인했다. 상세/지원조건의 실제 타입·코드 값·예외 관계·날짜 의미와 계정 접근은 미검증이다.

pilot 60문항·candidate_top_k=40·RRF 설정·근거 예산·recursive 분할은 제안이며 측정 최적값이 아니다. Embedding revision은 미확정, 검색/생성/availability evaluator/실DB annotation resolver는 후속이다. 합성 계약 두 case는 실제 품질 평가에 사용하지 않는다. 제품의 개인 전체 범위와 실제 coverage·초기 cohort를 혼동하지 않는다. 다음 작업의 선행 조건/파일/입출력/합격 기준/모델은 next_tasks T1/T2/T7/T4에 명시했다.

검증 후 Docker DB를 시작 때의 중지 상태로 복원했고 volume은 유지했다. 최종 branch/HEAD도 시작과 동일하다. 변경 사항은 작업 트리에 남겼으며 커밋·push하지 않았다.

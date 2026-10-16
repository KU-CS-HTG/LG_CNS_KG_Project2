# 후속 작업과 인계 계약

이 문서는 작업 명세이며 실행/위임 완료 보고가 아니다. 이번 작업에서 다른 에이전트를 실행하지 않았다. 새 스키마·자격·검색 설계는 Sol 6.1 High가 담당하도록 권장한다. Luna 6는 아래의 확정된 좁은 명세에만 배정한다. 개발 모델 선택은 런타임 모델 `gpt-4o-mini`를 변경하지 않는다.

## T1 — 보조금24 실제 응답 검증 (Sol 6.1 High)

- 목표/범위: 목록 1페이지 3건부터 수집하고, 대표 항목의 상세/지원조건을 소량 확인. 전국 전수 수집 제외.
- 선행 조건: 해당 API 활용신청 승인, Decoding 키를 `.env`/환경변수로 안전하게 공급, 계정 한도 확인. 현재 세 종류 키 모두 프로세스 미설정.
- 예상 파일: `gov24.py`, `domain.py`, `docs/data_sources.md`, `docs/source_specs.json`, 승인된 비식별 실응답 fixture. 필요한 경우 새 migration.
- 계약: 승인 endpoint 응답 bytes → raw ID + 검증된 필드/타입/빈값/코드 의미/날짜/ID 중복/지역·대상 범위 보고서. API key와 개인/연락처 정보는 fixture 공유 전 검토.
- 합격 기준: 실제 HTTP 상태·수집일·run ID를 기록하고 문서와 응답 차이를 명시. missing/false/empty 코드 의미를 확정하지 못하면 unresolved 유지. source URL 상태와 표본 범위를 명시. 기존 합성 fixture를 실제 응답으로 재명명하지 않음.
- 검증: 명시적 live 실행과 raw 재생 결과 비교, 기존 오프라인 및 PostgreSQL suite. 실접속 테스트는 기본 suite에서 분리.

## T2 — 상세 근거와 검증 규칙 계약 (Sol 6.1 High)

- 목표/범위: T1의 관측으로 목록/상세/지원조건 버전과 field evidence 연결을 확정. 개인/청년·고용·주거·복지 범위 분류. 코드 기반 자동 자격 단정 제외.
- 선행 조건: 실제 표본·코드 값 의미·지원조건 관계에 대한 근거. 보조금24 상세와 목록의 field 차이 정리.
- 파일: `domain.py`, `gov24.py`, `ingestion.py`, 새 migration, `tests/test_ingestion.py`, 아키텍처 문서.
- 계약: source별 명시적 record type → 검증 상태 포함 canonical revision; 조건은 원문·span·reviewer·적용 시점 연결. 같은 source ID의 부분 응답이 이미 있는 상세를 지우지 않도록 merge/version 규칙부터 정한다.
- 합격 기준: 실제 결측·nested·identifier 변이 대응, 목록/상세 충돌 기록, 미완료 수집 누락 무삭제, 코드 의미 미확인 시 확인 필요.
- 검증: frozen response fixtures, 변경/역변경(A→B→A), page 실패/DB rollback, 근거 span round-trip 및 migration 검증.

## T3 — 프로필 및 조건 evaluator 확대 (Sol 6.1 High)

- 목표/범위: 확인된 프로필과 검증 조건 tree, 지역·취업·연령 범위 및 명시된 접수 기간부터 구현. 복잡한 소득 규칙은 근거가 확보된 경우만.
- 선행 조건: T2 근거 계약, 정책 담당 검증/리뷰 기준, 평가 날짜/Asia/Seoul, 소득은 단위·산정 정의·연도/가구원 수를 갖춘 공식 기준표.
- 파일: `eligibility.py`, 새 profile/condition schema, 관련 migration(필요할 때만), `tests/test_eligibility.py`.
- 계약: `validated condition tree + user-confirmed facts + evaluation date -> leaf/group results + reasons + evidence IDs`; LLM interpretation은 별도 필드.
- 합격 기준: AND/OR/예외 구조 유지, 누락/모호함/현행 미검증 기준은 확인 필요. 소득 의미·단위 불일치 비교 금지. 공식 자격 결정으로 표현 금지. 신청 상태와 자격 분리.
- 검증: 경계·불완전·충돌·복합 조건 truth tables, 적용연도와 접수일 경계, 비호환 소득 case. 테스트는 외부 모델 불필요.

## T4 — 검색 실험 기반 (Sol 6.1 High)

- 목표/범위: frozen corpus에서 Kiwi/BM25Okapi, BGE-M3 dense, RRF 비교. 생성·reranker 제외.
- 선행 조건: T1/T2를 거친 실제 corpus snapshot과 관련 프로그램 라벨. 모델 다운로드 저장 공간·메모리 및 Windows CPU/CUDA 호환 확인.
- 파일: 새로운 `retrieval/`, `embedding/`, index manifest, vector/model metadata migration, `tests/`, `evaluation/`.
- 계약: `query + confirmed constraints + snapshot -> ranked program IDs with document IDs/ranks/evidence`; fixture namespace 제외, unknown 범위 유지.
- 합격 기준: tokenizer/사전 버전 고정, 모델 revision/dimension 확인, 자동 모델 다운로드 없음, BM25 전체 rebuild/원자 교체, 과거 revision 제외, chunk 수에 따른 중복 부풀림 방지, raw score 합산 금지.
- 검증: docs/evaluation.md의 동일 corpus/queries 비교와 mock embedding 단위 테스트. 실제 모델 실험은 별도 marker. 측정 전 개선 주장 금지.

## T5 — 좁은 운영/CI 작업 (Luna 6, 명세 고정 후)

- 목표/범위: 현재 lock과 README 명령 그대로 Windows Python 3.12 CI 및 Linux Python 3.11/3.12의 기본 suite 구성. 새 스키마/검색/자격 설계 제외.
- 선행 조건: 실행 환경별 명령 확정, CI 서비스 사용 가능, lock 변경 불필요 확인. 미지원 환경 문제는 Sol에 인계.
- 파일: `.github/workflows/tests.yml`, README의 CI 상태 설명.
- 계약: checkout → `uv sync --locked` → Ruff check/format check → 기본 pytest; API key/대형 모델/유료 호출 없음.
- 합격 기준: 각 matrix에서 실제 실행 로그 확인, PostgreSQL/live/paid/large_model 기본 제외, 저장소 비밀 없이 실행. 임의 의존성 upgrade 금지.
- 검증: CI workflow의 실제 run. 로컬 YAML 검사만으로 CI 통과라고 보고하지 않음.

## T6 — 생성 API·응답 계약 (Sol 6.1 High, 이후 좁은 구현만 Luna 가능)

- 목표/범위: 근거 목록에 한정한 gpt-4o-mini 생성과 citation validator, FastAPI 요청/응답. UI는 이후.
- 선행 조건: T3/T4, 공급 API 키/base URL의 모델 및 endpoint 호환 확인. 공식 모델 페이지 deprecation 표기를 공급 환경과 대조. 유료 호출 예산/승인 확보.
- 파일: `generation.py`, `api.py`, response schemas, prompt/evidence 검증 테스트.
- 계약: 질문·확인된 프로필·평가일·문서 IDs/허용 URLs → 관련성·자격 findings·확인 필요·신청 정보·freshness·claim-evidence links.
- 합격 기준: 모델은 지정 `gpt-4o-mini`, 모델/embedding endpoint 자동 대체 없음. 없는 URL/정책 정보 생성 차단, retrieved text 명령 격리, no-result/증거 부족/불완전 프로필 처리, 민감 프로필 로그 제외.
- 검증: mock generation와 공격 문서/허위 citation tests; 승인된 작은 paid suite 별도. SDK 설치 버전 API signature 확인. 실호출 없이 계정 접근 성공 주장 금지.

## Astra 검토가 유용할 수 있는 질문

현재 막힌 설계 충돌은 없으며 Astra 작업을 시작하지 않았다. 다음 상황이 실제 발생하면 좁은 review package를 준비한다.

1. 보조금24 supportConditions 코드와 상세 선정기준이 충돌할 때: 실제 raw IDs, 공식 코드표, 적용 시점, 두 해석의 삼값 결과를 제공하고 **“어떤 값을 검증된 hard constraint로 사용할 수 있으며 어떤 subtree를 unresolved로 남겨야 하는가?”**를 검토 요청.
2. 여러 source의 유사 프로그램이 중복되거나 지역/연도별로 달라질 때: source IDs·원문·신청 주체·기간·금액 근거를 제공하고 **“사용자 결과에서 어느 단위까지 묶을 수 있으며 provenance 손실 없는 관계 모델은 무엇인가?”**를 검토 요청.
3. RRF 후 긴 정책만 상위에 몰릴 때: 고정 corpus, chunk 분포, relevance 라벨, rank와 metric을 제공하고 **“프로그램 단위 집계의 어떤 방식이 coverage와 근거 다양성을 지키며 편향을 줄이는가?”**를 검토 요청.

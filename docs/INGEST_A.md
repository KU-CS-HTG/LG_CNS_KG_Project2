# A 수집·정제·SQLite·분할 운영 계약

2026-10-19 구현. 기존 `main`의 A 공개 함수와 `Chunk` 타입을 유지한다.
검증은 합성 응답·임시 SQLite·가짜 소비자에 한정하며 실운영 수집 및 실제 Vector/BM25 쓰기는 검증하지 않았다.

## API 확인 수준과 필드 매핑

공식 출처: [공공데이터포털 제공기관 페이지](https://www.data.go.kr/data/15113968/openapi.do).
목록·상세 제공, JSON/XML, 실시간 갱신 및 인증/호출 제한 오류 설명은 **문서 확인**이다.
해당 페이지에서 동적 Swagger의 상세 스키마를 읽지 못했고, 공식 OAS 접근도 실패했다.
라이브 요청은 승인되지 않아 실행하지 않았다. 아래는 기존 클라이언트와 프로젝트 계약을 구현한
**후보 매핑**이며 API 필드·ID·카디널리티를 실제 확인했다고 해석하면 안 된다.

| 엔드포인트 / 후보 원천 필드 | 정규 필드 | 결합 키·예상 카디널리티 | 원천 누락 처리 | 확인 수준 |
|---|---|---|---|---|
| serviceList / 서비스ID | service_id | 서비스ID, 목록 1 : 상세 0..1 : 조건 0..1 | 비어 있거나 중복 충돌이면 보류 | 미검증 |
| serviceList, serviceDetail / 서비스명 | service_name | 동일 ID | 이름이 없으면 해당 서비스 보류 | 미검증 |
| serviceDetail / 지원대상, 선정기준 | support_target, selection_criteria | 동일 ID | 성공한 상세 행의 빈 값·누락 키는 공백으로 갱신 | 미검증 |
| serviceDetail / 지원내용, 신청방법 | support_content, how_to_apply | 동일 ID | 위와 동일 | 미검증 |
| serviceList, serviceDetail / 신청기한 | application_period, deadline | 동일 ID | 원문 보존, 명확한 날짜만 ISO 종료일 | 미검증 |
| serviceList, serviceDetail / 소관기관명, 서비스분야 | agency, field | 동일 ID | 빈 값 보존, metadata에서 생략 | 미검증 |
| serviceList, serviceDetail / 상세조회URL | detail_url | 동일 ID | 생성하지 않음, HTTPS 정부 도메인만 허용, 누락 품질 경고 | 미검증 |
| supportConditions / 서비스ID, JA* | `_sources.supportConditions` | 동일 ID | 실패/미관측이면 기존 값 유지 | 필드·코드 의미 미검증 |
| 검증된 A 어댑터 / `_verified_eligibility` | eligibility, metadata | API 원본 필드가 아닌 A 내부 계약 | 미검증 JA* 코드를 자동 변환하지 않음 | 합성 테스트만 확인 |
| 각 엔드포인트 / page, perPage, currentCount, totalCount, 선택적 matchCount | Collection, 실행 요약 | 엔드포인트별 독립 페이지 | 누락·불일치·반복 페이지는 실패/부분 수집 | 실제 의미 미검증 |

`serviceDetail`도 기존 클라이언트가 사용하던 페이지 방식으로 수집하되, 특정 서비스 ID 질의가
가능하다고 가정하지 않는다. ID는 공백 없는 비어 있지 않은 문자열만 허용한다.
동일 ID의 완전히 같은 중복은 하나로 합치고 부분 수집으로 기록한다. 서로 다른 중복은 해당 ID
전체를 보류한다. 위치로 결합하지 않는다. 목록에 없는 상세/조건은 집계하고 활성 데이터에 넣지 않는다.

### 완전성·재시도·원본

연결/읽기 timeout은 10/30초. 최대 3회 시도하며 연결 오류, timeout, HTTP 408/429 및 일부 5xx,
API 코드 01/05/23 등 일시 오류에 1/2초 backoff를 적용한다. Retry-After 초/HTTP-date는 최대 30초로 제한한다.
인증/권한·일일 한도 오류는 재시도하지 않는다. API 메시지나 인증 URL을 로그에 출력하지 않는다.
잘못된 JSON, data 누락, 페이지 불일치는 정상 빈 데이터가 아니다.

`data/raw/<run_id>/`에 성공 수신한 페이지 envelope, 엔드포인트별 summary, 실행 summary를 저장한다.
나중 페이지 실패 전의 성공 페이지도 남긴다. 비정상 JSON/HTTP 응답 본문은 저장하지 않고 통제된
오류 코드·엔드포인트·수신 페이지 수·failed_page로 진단한다. 키·인증 URL은 마스킹한다.

상태는 `complete`, `sample`, `partial`, `failed`다. `complete`는 **후보 프로토콜의 건수 검증 통과**이며
실제 API 계약 확인과 다르다. `verified`를 별도 기록한다. `--sample-pages`가 지정되면 요청 페이지가 모두
성공해도 항상 `sample`이다. 총 건수 변화, 반복 페이지, ID 충돌, 의심스러운 빈 스냅샷은 소실 권한이 없다.

`client.VERIFIED_CONTRACTS`는 세 엔드포인트 모두 False다. 공식 명세와 승인된 소량 실측으로
각 엔드포인트의 ID/페이지 의미를 확인한 뒤 근거와 함께 별도 변경해야 한다. CLI에 검증 우회 옵션은 없다.
`mark_closed()`는 인자 없는 기존 인터페이스에서 안전하게 `[]`를 반환한다. 파이프라인의 A 내부
`apply_run()`만 **검증된·권위 있는·비표본·비어 있지 않은 완전 목록**을 받아 트랜잭션 안에서
소실을 판정한다. 청년 필터 전 전체 목록 ID와 비교하며 상세/조건 누락으로 소실을 판단하지 않는다.
최초 전체 0건의 권위성을 확인할 정책은 아직 없으므로 자동 전체 폐쇄를 지원하지 않는다.

## 정제·관측·청년 포함 정책

상세 성공 행이 있으면 네 본문 항목은 상세가 우선한다. 그 행에 키가 없거나 null/빈 값이면 기존
상세 값을 지운다. 기관·분야·기한·이름·URL은 상세에 해당 키가 있을 때 상세 우선, 그 외 목록을 쓴다.
상세 행 자체가 실패/누락이면 이전 정상 상세 원본을 재사용한다. 조건도 동일하다.
`_sources`에 엔드포인트별 마지막 성공 관측, `_observations`에 fresh/retained/unavailable을 저장한다.
원천의 빈 값과 수집 실패를 구분한다. 처음부터 없는 상세/조건은 새로 만들어 채우지 않는다.

개행·숫자·단위·부정·예외·목록을 보존하는 규칙 정제만 수행한다. LLM을 호출하지 않는다.
단일 전체 날짜 또는 전체 날짜~전체 날짜만 파싱한다. `상시`, `예산 소진 시`, `별도 공고`, 혼합/모호한 기간은
원문만 보존한다. UTC 시스템 날짜와 무관하게 만료 기준일은 한국 시간이다.

정형 코드북을 확인하지 못했으므로 JA0110/JA0111 등을 연령으로 추정하지 않는다. 본문의 자녀 나이,
복합 조건을 단일 신청자 나이로 추출하지 않는다. 검증된 어댑터 계약은 다음과 같다(현재 합성 테스트용):

```python
conditions = {"서비스ID": "A", "_verified_eligibility": {
    "source": "applicant", "age_ranges": [[19, 34]], "region": "전국"
}}
service = clean_service(raw, conditions)
```

이 객체는 실제 API 필드가 아니다. 다중 연령 구간은 eligibility에 보존하지만 공유 Metadata의 단일
age_min/age_max로 평탄화하지 않는다. 명시적인 전국은 `region='전국'`, 미상은 키 생략이다.
기관명에서 거주지를 추정하지 않는다. age/region은 검증된 어댑터 결과만 사용한다. 모든 미상 metadata 키는 생략한다.
기관/링크가 없으면 data_quality와 실행별 품질 건수로 드러내며 거짓 출처를 만들지 않는다.

`is_youth_service()`는 기존 bool 반환을 유지한다. 잠정 정책은 검증된 신청자 연령 구간 중 하나가
`config.YOUTH_AGE_RANGE`와 겹치거나, 서비스명/지원대상/선정기준에 `config.YOUTH_KEYWORDS`가 있는 경우다.
수집 나이를 개별 자격에 채우지 않는다. `youth_inclusion_reasons()`로 연령 겹침, 키워드·필드,
근거 없음의 이유를 얻는다. 18~65세 일반 성인 프로그램이나 일반 구직 상담도 포함될 수 있다.
요약의 broad_age_examples/broad_keyword_examples에 대표 사례를 남긴다. **정책 확정은 팀 합의 대기**다.

누락 상세/조건으로 이전 청년 서비스가 갑자기 범위 밖으로 판단되면 갱신을 유보한다. 소실/범위 이탈로
비활성화된 서비스의 재개도 보존된 옛 본문만으로 허가하지 않고 신선한 보조 관측을 기다린다.
신규 청년 키워드가 명시된 서비스는 보조 관측 누락을 기록하며 포함할 수 있다.
비청년 서비스는 검색에서 제외하고 ID·이름·기관·URL·해시·판정 이유 정도의 상태 감사 정보만 남긴다.
원본은 별도 raw 진단에 보존된다. 만료/소실된 청년 레코드는 근거와 재개를 위해 본문도 보존한다.

## SQLite·분할·증분 상태

설정은 기존 `config.DB_PATH`, 표준 sqlite3를 쓴다. 테이블은 services(정규 JSON·해시·상태·버전),
ingestion_runs(완전성 포함 요약), pending_changes(서비스별 최신 미확인 이벤트) 세 개다.
상태/원인: active 또는 inactive, `expired`/`api_disappeared`/`youth_scope_exit`를 구분한다.
본문·이름·기관·URL·기간 원문·검증 자격·metadata의 정렬 JSON SHA-256으로 변경을 감지한다.
수집 시각·fresh/retained 같은 운영 정보는 해시에서 제외한다. metadata만 바뀌어도 갱신된다.

DB 갱신과 pending 기록, 완료 실행 요약은 한 트랜잭션이다. 중간 실패는 롤백하고, 수집 중 중단은
다음 실행 시작 시 interrupted로 기록한다. 프로세스 파일 잠금으로 수집/upsert 중첩을 막고
프로세스 사망 시 OS가 잠금을 해제한다. DB 파일이나 이력을 초기화하지 않는다.

`load_services(None)`는 활성 청년 서비스 전체, `load_services([])`는 빈 목록이다.
`sql_keyword_search(keywords, k)`는 활성 레코드 이름·기관·네 본문 항목에 literal OR LIKE를 적용하며
%, _, 역슬래시를 escape한다. SQLite의 기본 LIKE 대소문자 규칙(ASCII case insensitive)을 따른다.
서비스 ID 순서와 최대 k를 사용하며 검색 순위·정형 필터 실험은 B 소유다.

SECTIONS 네 개만 분할한다. 각 text는 `서비스명 + '\n' + 본문`이다. 전체 text가 max_chars 이하다.
문단→줄→문장→고정 길이로 재귀 분할하고 구분자를 포함하여 모든 정규 본문을 보존한다.
빈 항목은 생략하고 `service_id#section`, 분할 시 `service_id#section-n`을 사용한다.
서비스명 접두 이후 본문 한 글자도 못 넣는 한도와 양의 정수가 아닌 한도는 ValueError다.

## B 인계: 구현된 A API와 합의가 필요한 B 적용

`pipeline.run(full=False) -> list[Chunk]`는 **모든 현재 미확인 replace 이벤트**의 조각을 반환한다.
이번 수집 변경 0건이어도 이전 미확인 조각이 재전달된다. 반환/직렬화는 확인 응답이 아니다.
`--full`은 현재 활성 서비스 전체 조각을 재생성해 pending에 넣는다. DB·이력·기존 제거 이벤트를 보존한다.
부분 수집을 완전 수집으로 승격하지 않는다. 내용 버전이 같아도 새 이벤트 ID로 이전 확인 응답을 구분한다.

```python
from src.ingest import store

events = store.get_pending_changes()  # serializable dicts, includes full chunks even []
removal_ids = store.pending_removal_ids()
chunks = store.pending_chunks()      # existing Chunk type
for event in events:
    # Proposed B implementation: replace ALL chunks of event['service_id'], or remove.
    # Persist matching service version/event in BOTH Vector and BM25, then:
    store.acknowledge_changes([event])
```

마지막 줄은 두 색인 적용이 성공한 뒤에만 호출한다. 위 예시는 적용 순서를 설명하는 계약이며 실제 B writer가 아니다.
토큰은 service_id+version+event_id다. 오래된 토큰은 새 pending을 지우지 못한다. 반환값은 실제 확인된 건수다.
서비스별 최신 이벤트가 이전 이벤트를 대체하므로 소비자는 이벤트 스냅샷의 전체 교체를 멱등 적용해야 한다.
빈 조각 replace도 기존 전체 삭제를 의미한다. remove는 이유와 서비스 ID를 갖는다.

**구체적 계약 충돌/제안:** 현재 B `vector_store.build_index()` docstring의 chunk_id upsert만으로는
청크 수 감소·빈 항목 전환 때 오래된 조각이 남는다. 영향 호출자는 B 색인 writer와 재시도 소비자다.
호환 대안은 기존 Chunk 타입/빌더를 유지하면서 B가 서비스 단위 삭제 후 전체 조각을 적용하고, Vector/BM25에
동일 버전이 반영된 후 A 확인 API를 호출하는 것이다. 버전 fencing/소비자 단일 실행, 두 색인의 장애 복구 및
출처 누락 항목의 C 표시 방침은 **팀 결정 필요**다. 코드/문서 작성이 합의를 의미하지 않는다.
가짜 소비자만 tests/ingest/test_store_pipeline.py에서 검증했으며 B/C 코드는 수정하지 않았다.

## 실행·테스트·일일 스케줄

PowerShell, 현재 workspace에서 실행한다. 실제 API 명령은 사용자 승인 후에 실행한다.

```powershell
.\.venv\Scripts\python -m src.ingest.pipeline
.\.venv\Scripts\python -m src.ingest.pipeline --full
.\.venv\Scripts\python -m src.ingest.pipeline --sample-pages 1 --per-page 2
# 아래는 네트워크 없이 합성 데이터를 사용한다. 기본 DB에 합성 데이터가 들어가므로 테스트에는 임시 DB를 사용한다.
.\.venv\Scripts\python -m src.ingest.pipeline --offline-fixture tests/fixtures/a_synthetic_api.json
.\.venv\Scripts\python -m pytest tests/ingest -q -ra
.\.venv\Scripts\python -m pytest -q -ra
```

기본 및 --full CLI, 임시 DB 격리 예시는 A 테스트에 있다. `complete/sample` CLI 종료 코드는 0,
`partial/failed`는 1, 잘못된 인수는 2다. 원본 복사 저장 실패가 DB 커밋 뒤 발생하면 DB 요약을 우선한다.
테스트는 키·임베딩·LLM 없이 동작한다. A 로컬 훅은 NotImplementedError를 실패로 유지하며 공용 skip 훅은 건드리지 않는다.
Windows 샌드박스의 pytest 임시 폴더 ACL 제한이 있으면 승인된 일반 터미널에서 실행하거나 접근 가능한 임시 경로를 쓴다.

일일 실행은 Windows 작업 스케줄러에서 매일 트리거, 프로그램에 현재 workspace의
`.venv\Scripts\python.exe`, 인수 `-m src.ingest.pipeline`, 시작 위치에 workspace 절대 경로를 지정한다.
키는 .env/환경에 유지하고 명령 인수에 넣지 않는다. 작업 등록·활성화는 이번 작업에서 하지 않았다.

## 남은 검증

공식 v3 스키마·ID·엔드포인트별 페이지/필터·JA 코드북을 확보하고 승인된 소량 API 표본으로 확인해야 한다.
그 전에는 구조화 연령/지역 자동 추출과 자동 API 소실 판정을 운영에 사용하지 않는다.
실제 API 응답, 전체 청년 건수, 운영 처리량, Vector/BM25 통합, 검색·답변 평가지표는 미검증이다.
기존 B/C fixtures와 eval 질문은 교체하지 않았으며 실제 수집 후 팀원이 정답·공식 근거를 확인해야 한다.

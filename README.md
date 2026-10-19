# 개인 맞춤형 정부 지원제도 탐색 및 안내 서비스 (RAG)

사용자가 생활 상황을 자연어로 질문하면(예: "서울에 사는 27살 취업준비생이 받을 수 있는 청년 지원 제도"), 공공데이터에서 해당 가능성이 있는 정부 지원제도를 찾아 **자격조건·신청 절차를 출처와 함께** 안내하는 RAG 서비스입니다. **1차 범위는 청년층 대상 제도**입니다([1.1](#11-데이터-범위--청년층-우선)).

- 설계 근거: [`docs/사전기획서.docx`](docs/사전기획서.docx) (1~9장)
- 상세 설계 이유·변경 이력: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- AI 작업 지침(규칙 원문): [`CLAUDE.md`](CLAUDE.md)

> A의 수집·정제·SQLite·분할·증분 인계는 구현되어 있으며 오프라인 합성 테스트로 검증했습니다. 실제 API 세부 계약은 미검증이고 자동 소실 처리는 차단되어 있습니다. B/C는 스켈레톤입니다. 구현된 동작·필드 확인 수준·실행·B 인계는 [A 운영 계약](docs/INGEST_A.md)을 참고하세요.

## 1. 데이터

- 출처: 공공데이터포털 "행정안전부_대한민국 공공서비스(혜택) 정보"(보조금24) REST API (전체 약 1만 건 중 **청년 대상 제도만 사용**, 건수는 수집 후 확정)
- 서비스 1건 = 서비스명, 지원대상, 선정기준, 지원내용, 신청방법, 신청기한, 소관기관, 상세 링크 + 지원조건(정형)
- 원본 응답은 `data/raw/`에 저장하고 **커밋하지 않습니다**. API 키는 `.env`에만 두고 **커밋하지 않습니다**.
- 첨부 문서·이미지는 수집하지 않고 상세 링크로 대신합니다.

### 1.1 데이터 범위 — 청년층 우선

프로젝트는 우선 **청년층 관련 제도**를 기준으로 진행합니다. 기획서의 대표 사용자(취업을 준비하는 청년, 신혼부부 등) 중 청년을 먼저 다루고, 구조는 다른 대상으로 확장할 수 있게 둡니다.

| 항목 | 내용 | 상태 |
|---|---|---|
| 범위 | 보조금24 전체(약 1만 건) 중 청년 대상 서비스만 DB·색인에 적재 | 결정 |
| 청년의 나이 | 청년기본법은 만 19~34세지만 지자체 조례는 만 18~39세까지 다양 → 수집은 넓게 `config.YOUTH_AGE_RANGE = (19, 39)`로 잡고, 개별 제도의 정확한 연령은 `Metadata.age_min/age_max`와 사용자 나이 필터(`Filters.age`)로 판단 | **제안, 팀 합의 필요** |
| 판정 방법 | 지원조건의 연령 범위가 위 구간과 겹치거나, 서비스명·지원대상·선정기준에 `config.YOUTH_KEYWORDS`(청년·대학생·사회초년생·취업준비·구직 등)가 있으면 청년 대상. 연령도 키워드도 없으면 제외 (`ingest.store.is_youth_service`, A 담당) | **제안, 팀 합의 필요** |
| 범위 밖 질문 | 예: "65세인데 받을 수 있는 연금은?" → 색인에 근거가 없으므로 기존 규칙대로 **모른다고 답하고 담당 기관·공식 사이트를 안내** (`no_evidence`). 청년 전용 서비스라는 안내 문구를 둘지는 C가 결정 | 제안 |
| 평가 질문 | `eval/questions.jsonl`은 청년 상황형 질문(구직·주거·교육·금융·신혼 등) 중심으로 작성하고, 범위 밖·근거 없음 질문도 일부 포함 | 제안 |
| 확장 | 판정 기준만 바꾸면(`is_youth_service`, `config`) 다른 대상(신혼·양육·노인 등)으로 넓힐 수 있게 범위 판정을 한 곳에 모았다 | - |

- 범위가 바뀌면 설계가 바뀐 것이므로 기획서 **8.3 기술 선택 기록**에도 적습니다 (예시는 `docs/ARCHITECTURE.md` §3.1 참고).
- 테스트용 샘플(`tests/fixtures/`)에는 청년이 아닌 제도(기초연금 등)도 섞여 있습니다. 실제 적재 데이터에는 들어가지 않지만 B의 필터가 연령 조건을 걸러내는지 시험하는 **부정 사례**로 남겨 둡니다.

## 2. 전체 구조 — 색인과 질의응답

![RAG 전체 구조](docs/images/rag-structure.png)

색인은 데이터가 바뀔 때만 다시 돌리고, 질문은 온라인 경로만 돕니다.

**오프라인 색인** (데이터가 바뀔 때만)

| 단계 | 내용 | 코드 | 담당 |
|---|---|---|---|
| ① 로드 | API 수집·정제 → DB 적재 | `src/ingest/client.py`, `store.py` | A |
| ② 분할 | 항목별(지원대상/선정기준/지원내용/신청방법) 구조 기반 분할, 긴 항목은 Recursive 보조, 조각 앞에 서비스명 접두 | `src/ingest/chunker.py` | A |
| ③ 임베딩 | 로컬 다국어 모델(bge-m3 우선, 자원 부족 시 multilingual-e5) | `src/index/embedder.py` | B |
| ④ 벡터 저장소 | 조각 + 메타데이터(지역·소관기관·분야·신청기한·대상조건) | `src/index/vector_store.py` | B |

**온라인 질의응답** (질문마다)

| 단계 | 내용 | 코드 | 담당 |
|---|---|---|---|
| 조건 추출 | 질문 → LLM이 나이·거주지·상황·관심 분야 추출 | `src/generate/condition_extractor.py` | C |
| ⑤ 검색 | 정형 필터 + Hybrid(Vector + BM25), Top-5, 중복 제거 | `src/retrieve/hybrid.py`, `filters.py` | B |
| 근거 선별 | 조각을 서비스 단위로 묶어 3~5개 제도로 제한 | `src/generate/evidence.py` | C |
| ⑥ 생성 | gpt-4o-mini → 답변 + 출처 | `src/generate/generator.py`, `citation.py` | C |

구조도와 다른 점: 입력이 PDF가 아닌 API이고 정형 지원조건이 함께 들어옵니다 / 검색 앞에 조건 추출과 정형 필터가 있습니다 / 검색과 생성 사이에 근거 선별이 있습니다.

**데이터 흐름**
- 오프라인(기본 1일 1회 증분): `ingest.client` → `ingest.store`(정제·DB·변경 감지) → `ingest.chunker`(`Chunk`) → `index.embedder` → `index.vector_store`
- 온라인(질문마다): 질문 → `condition_extractor`(`Filters`) → `retrieve.hybrid.search`(필터 + Vector + BM25, 중복 제거, 필요 시 필터 완화) → `evidence.select_evidence` → `generator.generate_answer` + `citation` → `Answer`

### 2.1 단계별 입출력 예시

> 아직 구현 전이라 **예상 출력**입니다. 형식(키 이름·타입)은 `src/common/schema.py`와 같고, 수치·문구는 이해를 돕기 위한 예시입니다. 구현하면서 실제 출력으로 교체하세요.
> 예시 전체가 한 질문의 흐름을 따라갑니다: **"서울에 사는 27살 취업준비생인데 월세가 부담돼요. 받을 수 있는 지원이 있을까요?"**

#### ① 로드 — `python -m src.ingest.pipeline`

수집 → 정제 → DB 적재 순으로 진행합니다. 기본 반환은 신규·변경 및 이전 실행의 **미확인 pending 조각**입니다. 아래 로그는 초기 설계의 예상 예시이며 실제 수집 결과가 아닙니다. 현재 한국어 실행 요약과 완전성 판정은 [A 운영 계약](docs/INGEST_A.md)을 따릅니다.

```
[client] serviceList 수집 완료: 10,213건 (perPage=500, 21페이지)
[client] supportConditions 수집 완료: 10,198건
[client] 원본 저장: data/raw/serviceList.json, supportConditions.json
[store]  청년 대상 필터: 10,213건 → 1,850건 (나머지는 DB·색인에서 제외)
[store]  upsert: 신규 40 / 변경 12 / 변동 없음 1,798
[store]  종료 처리: 3건 (색인에서 제거 대상)
```

DB에 저장되는 서비스 1건(정제 결과 `dict`)은 다음과 같습니다.

```json
{
  "service_id": "SVC000123",
  "service_name": "청년월세 지원(서울)",
  "agency": "서울특별시",
  "field": "주거",
  "support_target": "서울에 거주하며 월세로 사는 만 19~34세 무주택 청년",
  "selection_criteria": "부모와 따로 거주하고 소득이 기준 이하인 경우 선정",
  "support_content": "월 월세의 일부를 일정 기간 지원",
  "how_to_apply": "복지포털에서 온라인 신청",
  "deadline": "2026-12-31",
  "age_min": 19, "age_max": 34, "region": "서울",
  "detail_url": "https://www.gov.kr/portal/rcvfvrSvc/dtlEx/SVC000123",
  "content_hash": "a3f9...c21"
}
```

- 위 DB JSON은 초기 설계 예시입니다. 실제 SQLite는 정규 payload·실행 이력·미확인 인계 세 테이블을 사용하고, 검증된 자격은 eligibility에 보존합니다. 정확한 계약은 [A 운영 계약](docs/INGEST_A.md)을 따릅니다.
- 신청기한이 상시·미정이면 `deadline`은 `null`입니다. 값을 알 수 없는 항목도 `null`로 두며(추측 금지), 이는 뒤에서 "확인 필요"로 이어집니다.

#### ② 분할 — `chunker.chunk_service(service)`

서비스 1건이 항목별 최대 4개의 `Chunk`가 됩니다. 조각 앞에 서비스명이 붙고, 긴 항목은 `-1`, `-2`로 더 쪼개집니다.

```
입력: service_id=SVC000123 (청년월세 지원(서울))
출력: 4개 Chunk
  SVC000123#지원대상  "청년월세 지원(서울)\n서울에 거주하며 월세로 사는 만 19~34세 무주택 청년"
  SVC000123#선정기준  "청년월세 지원(서울)\n부모와 따로 거주하고 소득이 기준 이하인 경우 선정"
  SVC000123#지원내용  "청년월세 지원(서울)\n월 월세의 일부를 일정 기간 지원"
  SVC000123#신청방법  "청년월세 지원(서울)\n복지포털에서 온라인 신청"
```

조각 1개는 아래 형태입니다 (A → B 인터페이스).

```json
{
  "chunk_id": "SVC000123#지원대상",
  "service_id": "SVC000123",
  "service_name": "청년월세 지원(서울)",
  "section": "지원대상",
  "text": "청년월세 지원(서울)\n서울에 거주하며 월세로 사는 만 19~34세 무주택 청년",
  "metadata": {"region": "서울", "agency": "서울특별시", "field": "주거",
               "deadline": "2026-12-31", "age_min": 19, "age_max": 34, "target_tags": ["청년"]},
  "detail_url": "https://www.gov.kr/portal/rcvfvrSvc/dtlEx/SVC000123"
}
```

전체 규모는 청년 대상 서비스 수천 건 → 조각 수천~수만 개로 예상하며, 수집 후 실제 수치를 기록합니다.

#### ③ 임베딩 — `embedder.embed_texts(texts)`

텍스트 N개를 넣으면 같은 순서로 벡터 N개가 나옵니다. bge-m3 기준 벡터 차원은 1024입니다.

```
입력: ["청년월세 지원(서울)\n서울에 거주하며 ...", "청년월세 지원(서울)\n부모와 따로 ..."]
출력: [[0.012, -0.034, 0.101, ... (1024개)], [0.008, -0.029, 0.095, ...]]   # list[list[float]]
```

#### ④ 벡터 저장소 — `vector_store.build_index(chunks)` / 조회

색인은 조각을 `chunk_id` 기준으로 upsert하고, 조회는 `(Chunk, 점수)` 목록을 돌려줍니다.

```
[index] upsert 조각 208개 (신규·변경 서비스분) / 제거 12개 (종료 서비스분)
[index] 총 조각 수: 7,400

vector_search("월세가 부담돼요", k=3)
  → [(SVC000123#지원대상, 0.82), (SVC000456#지원대상, 0.80), (SVC000123#지원내용, 0.77)]
bm25_search("청년월세", k=3)
  → [(SVC000123#지원대상, 11.4), (SVC000456#지원대상, 10.9), (SVC000789#지원내용, 6.2)]
```

#### 조건 추출 — `condition_extractor.extract_conditions(question)`

질문에서 찾은 항목만 담고, 없는 항목은 키를 생략합니다. `to_filters`가 B의 `Filters`로 바꿉니다.

```
입력: "서울에 사는 27살 취업준비생인데 월세가 부담돼요. 받을 수 있는 지원이 있을까요?"
출력: {"age": 27, "region": "서울", "situation": ["구직"], "field": "주거"}
Filters: {"age": 27, "region": "서울", "field": "주거", "situation": ["구직"], "today": "2026-10-19"}
```

#### ⑤ 검색 — `hybrid.search(query, filters, k=5)`

정형 필터 후 Vector + BM25 결과를 합쳐 서비스 단위로 중복을 제거한 `list[Chunk]`를 돌려줍니다.

```
[retrieve] 필터 적용: 후보 조각 7,400 → 1,130 (기한 지난 제도 제외, 값이 빈 항목은 통과)
[retrieve] Hybrid Top-5 (서비스 중복 제거 후)

순위  service_id   서비스명               섹션       점수
 1    SVC000123    청년월세 지원(서울)    지원대상   0.91
 2    SVC000456    구직촉진수당           지원대상   0.84
 3    SVC000789    서울 청년수당          선정기준   0.80
 4    SVC000321    주거급여               지원대상   0.76
 5    SVC000654    국민내일배움카드       지원내용   0.71
```

결과가 없으면 `filters.relax()`로 필터를 한 단계씩 풀어 다시 검색합니다 (로그에 `[retrieve] 결과 없음 → 필터 완화: field 제거`처럼 표시).

#### 근거 선별 — `evidence.select_evidence(chunks)`

같은 서비스의 조각을 묶어 최대 5개 제도로 제한합니다. 키는 `service_id`입니다.

```
{
  "SVC000123": [지원대상 조각, 선정기준 조각, 지원내용 조각, 신청방법 조각],
  "SVC000456": [지원대상 조각, 선정기준 조각],
  "SVC000789": [선정기준 조각, 신청방법 조각],
  ...
}
```

#### ⑥ 생성 — `generator.answer_question(question)` → `Answer`

사용자에게 보이는 최종 결과입니다. 아래는 `Answer.text`의 모습입니다.

```
질문하신 상황(서울 거주, 27세, 구직 중, 월세 부담)에 해당할 수 있는 지원제도입니다.

1. 청년월세 지원(서울) — 서울특별시
   - 해당 가능 이유: 서울에 거주하는 만 19~34세 무주택 청년이 대상입니다.
   - 지원내용: 월 월세의 일부를 일정 기간 지원
   - 신청방법·기한: 복지포털 온라인 신청 / 2026-12-31까지
   - 확인 필요: 부모와 따로 거주하는지, 소득 기준 충족 여부
   - 근거: "서울에 거주하며 월세로 사는 만 19~34세 무주택 청년"
   - 상세 링크: https://www.gov.kr/portal/rcvfvrSvc/dtlEx/SVC000123

2. 구직촉진수당 — 고용노동부
   - 해당 가능 이유: 취업을 준비하는 저소득 구직자가 대상입니다.
   - 확인 필요: 가구 소득·재산 요건, 신청기한(정보 없음)
   ...

※ 최종 신청 자격은 담당 기관에서 확인하세요.
```

- 구조화된 결과는 `Answer.recommendations`(제도별 `Recommendation`)에, 근거는 각 `Recommendation.citations`에 들어 있어 UI가 따로 표시할 수 있습니다.
- 근거가 없는 질문(예: "내년에 새로 생길 예정인 청년 지원금은 어떻게 신청하나요?")은 추측하지 않고 `no_evidence=True`로 이렇게 답합니다.

```
수집된 자료에서 근거를 찾지 못했습니다. 담당 기관 또는 정부24 등 공식 사이트에서 확인해 주세요.
```

### 2.2 팀 간 인계 데이터 — A → B, C ↔ B

2.1의 예시 중 **사람과 사람 사이를 넘어가는 부분**만 따로 모았습니다. 두 곳 모두 같은 타입 `Chunk`(`src/common/schema.py`)를 주고받고, 달라지는 것은 **개수**(전체 vs 질문과 관련된 몇 개)입니다.

```
 A (src/ingest)                 B (src/index, src/retrieve)                 C (src/generate)
 ┌───────────────┐  list[Chunk]  ┌──────────────────────────┐  list[Chunk]  ┌───────────────────┐
 │ pipeline.run()│ ────────────▶ │ build_index(chunks)      │               │ answer_question() │
 │ 수만 개, 전부 │  (1) 색인용   │        ⋮                 │ ◀──────────── │  Filters          │
 └───────────────┘               │ search(query, filters)   │  (2) 필터     └───────────────────┘
                                 │                          │ ────────────▶          │
                                 └──────────────────────────┘  (3) Top-5, 이 질문에 맞는 것만
```

| | 누가 → 누구에게 | 무엇을 | 언제 | 개수 |
|---|---|---|---|---|
| (1) | A → B | `list[Chunk]` (`pipeline.run()`의 반환값) | 데이터가 바뀔 때(기본 1일 1회) | 신규·변경분 수백 ~ 전체 수만 개 |
| (2) | C → B | `Filters` (질문에서 뽑은 조건) | 질문마다 | 1개 |
| (3) | B → C | `list[Chunk]` (`search()`의 반환값) | 질문마다 | 최대 5개(서비스당 1개) |

#### (1) A → B: 색인용 조각

A가 B에게 주는 것은 **정제·분할이 끝난 서비스별 전체 교체 조각**입니다. `pipeline.run()`은 미확인 조각도 재전달하며 제거 이벤트는 별도로 조회합니다. B가 chunk_id upsert만 수행하면 줄어든 조각이 남을 수 있습니다. 서비스 전체 교체/제거 및 두 색인 적용 후 확인 응답을 제안하며, B writer 연동은 아직 구현·합의되지 않았습니다. [인계 API](docs/INGEST_A.md)를 따릅니다.

```python
# A가 만들어서 넘긴다 — pipeline.run() 의 반환값 (일부)
[
  Chunk(chunk_id="SVC000123#지원대상", service_id="SVC000123", service_name="청년월세 지원(서울)",
        section="지원대상",
        text="청년월세 지원(서울)\n서울에 거주하며 월세로 사는 만 19~34세 무주택 청년",
        metadata={"region": "서울", "agency": "서울특별시", "field": "주거",
                  "deadline": "2026-12-31", "age_min": 19, "age_max": 34, "target_tags": ["청년"]},
        detail_url="https://www.gov.kr/portal/rcvfvrSvc/dtlEx/SVC000123"),
  Chunk(chunk_id="SVC000123#선정기준", ... 같은 서비스의 다른 항목 ...),
  Chunk(chunk_id="SVC000456#지원대상", service_id="SVC000456", service_name="구직촉진수당",
        section="지원대상", text="구직촉진수당\n취업을 준비하는 저소득 구직자",
        metadata={"agency": "고용노동부", "field": "일자리", "age_min": 15, "age_max": 69},  # region·deadline 없음 = 값이 비어 있음
        detail_url="https://www.gov.kr/portal/rcvfvrSvc/dtlEx/SVC000456"),
  ...
]
```

- **A가 보장할 것**: `chunk_id`는 전체에서 유일(`{service_id}#{section}`), `text` 맨 앞에 서비스명, `section`은 4개 항목 중 하나, 값을 모르는 `metadata` 키는 **생략**(추측해서 채우지 않음).
- **B가 가정해도 되는 것**: 위 규칙이 지켜진다는 점, 같은 `service_id`의 조각이 여러 개라는 점. 반대로 **가정하면 안 되는 것**: 모든 조각에 `region`·`deadline`·`age_min`이 있다는 점(비어 있으면 필터에서 제외하지 않고 통과시킴).
- 둘이 함께 확인하는 방법([B가 A 없이 테스트하기](#b가-a의-구현-없이-테스트하기)): A가 실제 조각 몇 건을 `tests/fixtures/sample_chunks.json`과 같은 형식으로 저장해 B에게 주면, B는 `python -m pytest`에서 그 파일로 바로 시험할 수 있습니다.

#### (2) C → B: 필터

C는 질문에서 뽑은 조건을 `Filters`로 바꿔서 B에게 줍니다. **값을 못 찾은 키는 생략**합니다.

```python
{"age": 27, "region": "서울", "field": "주거", "situation": ["구직"], "today": "2026-10-19"}
```

#### (3) B → C: 이 질문에 맞는 조각 Top-5

> B 없이 이 인계를 흉내 내서 C를 시험하는 방법은 [테스트](#c가-b의-구현-없이-테스트하기)를 보세요. 샘플은 `tests/fixtures/sample_search_results.json`입니다.

B가 C에게 주는 것은 **질문 한 건에 대해 필터·검색·중복 제거를 거친 조각 목록**입니다. 타입은 (1)과 같고, 서비스당 1개로 줄어 있습니다.

```python
# B: hybrid.search("월세가 부담돼요 ...", filters, k=5)  →  C가 받는 값
[
  Chunk(chunk_id="SVC000123#지원대상", service_name="청년월세 지원(서울)", section="지원대상",
        text="청년월세 지원(서울)\n서울에 거주하며 월세로 사는 만 19~34세 무주택 청년", ...),
  Chunk(chunk_id="SVC000456#지원대상", service_name="구직촉진수당", section="지원대상",
        text="구직촉진수당\n취업을 준비하는 저소득 구직자",
        metadata={"agency": "고용노동부", "field": "일자리", "age_min": 15, "age_max": 69}, ...),  # region·deadline 없음
  Chunk(chunk_id="SVC000789#선정기준", service_name="서울 청년수당", section="선정기준", ...),
  Chunk(chunk_id="SVC000321#지원대상", service_name="주거급여", section="지원대상", ...),
  Chunk(chunk_id="SVC000654#지원내용", service_name="국민내일배움카드", section="지원내용", ...),
]   # 관련도 높은 순
```

- **B가 보장할 것**: 순서는 관련도 순, 같은 서비스의 조각은 중복 제거, 신청기한이 지난 제도는 없음, 결과가 없으면 필터를 풀어 다시 검색한 뒤 돌려줌(그래도 없으면 빈 리스트).
- **초기 C 계약의 미해결 기대값**: 모든 조각의 기관·상세 링크가 비어 있지 않다는 기대와 달리 원천에 누락이 있을 수 있습니다. A는 값을 만들지 않고 metadata 키 생략/빈 detail_url 및 품질 진단으로 표시합니다. 공용 타입과 C 코드는 유지했으며 누락 표시·후보 제외 방침은 팀 결정이 필요합니다. region·deadline 등 미상 조건은 기존처럼 "확인 필요" 대상입니다. 이 제안은 팀 합의 완료를 의미하지 않습니다.
- C는 이 목록에서 같은 서비스의 다른 항목(선정기준, 신청방법 등)이 더 필요하면 `service_id`로 조각을 더 요청하거나 DB를 조회해야 합니다. 어느 쪽으로 할지는 B·C가 합의해서 정합니다. (근거 선별 단계가 이 문제를 다룹니다.)

> 형식이나 보장 사항을 바꾸고 싶으면 `src/common/schema.py`와 이 절을 먼저 고치고 상대 담당자와 합의하세요.

## 3. 디렉터리 구조와 담당

```
src/
  common/    config.py, schema.py                                  # 전원 공용: 설정, 모듈 간 데이터 계약
  ingest/    client, normalize, store, chunker, pipeline           # A
  index/     embedder, vector_store                                # B
  retrieve/  hybrid, filters, baseline                             # B
  generate/  condition_extractor, evidence, generator, citation    # C
app/         main.py                                               # C: UI
eval/        metrics, answer_eval, run_eval, questions.jsonl, results/
tests/       fixtures/(가상 샘플 조각·질의·검색 결과), conftest.py(fake_search 등), test_*.py
data/raw/    원본 응답 (커밋 금지)
docs/        사전기획서.docx, ARCHITECTURE.md, images/
```

| 담당 | 맡은 단계 | 디렉터리 |
|---|---|---|
| A (데이터·색인) 담당자: | ① 로드, ② 분할, 메타데이터 설계, 정기 수집 | `src/ingest/` |
| B (임베딩·검색) 담당자: | ③ 임베딩, ④ 벡터 저장소, ⑤ 검색, 정형 필터, Baseline, 검색 평가 | `src/index/`, `src/retrieve/`, `eval/metrics.py`·`eval/run_eval.py` |
| C (생성·서비스) 담당자: | 조건 추출, 근거 선별, ⑥ 생성, 인용 형식, UI, 답변 평가 | `src/generate/`, `app/`, `eval/answer_eval.py` |

담당자 이름은 팀에서 채웁니다. 평가 질문 세트(`eval/questions.jsonl`)와 기획서 8.3 기술 선택 기록은 전원이 함께 작성합니다.

## 4. 모듈 간 인터페이스

팀 합의 후 확정합니다. 바꿀 때는 `CLAUDE.md`와 이 문서부터 고치고, 타입은 모두 `src/common/schema.py`에 있습니다.

- **A → B**: 조각 `Chunk{chunk_id, service_id, service_name, section, text, metadata, detail_url}`
  - `metadata`: 지역, 소관기관, 분야, 신청기한, 연령 범위, 대상 태그
- **C → B**: `Filters` (나이·지역·분야·상황·기준일, 값이 없으면 키 생략)
- **B → C**: `search(query, filters, k=5, mode="hybrid_filter") -> list[Chunk]` (`mode`는 평가 실험용, 서비스는 기본값 사용)
- **C → 사용자**: `Answer` = 답변 텍스트 + 제도별 `Recommendation`(해당 가능 이유, 지원내용, 신청방법·기한, 확인 필요 조건, 근거 목록[서비스명·소관기관·근거 문구·상세 링크]) + `no_evidence`

## 5. 답변 생성 규칙

- 검색된 근거 안에서만 답합니다. 근거가 없으면 모른다고 답하고 담당 기관·공식 사이트 확인을 안내합니다.
- 자격이 불명확하거나 정형 조건 값이 비어 있으면 단정하지 않고 "확인 필요"로 표시합니다.
- 모든 답변에 서비스명·소관기관·근거 문구·상세 링크를 붙이고, "최종 신청 자격은 담당 기관에서 확인" 안내를 넣습니다.
- 신청기한이 지난 제도는 후보에서 제외합니다. 정형 필터는 하드가 아니라 소프트로 적용하고, 결과가 없으면 필터를 풀어 다시 검색합니다.
- 사용자에게 보이는 문구는 한국어로 작성합니다.

## 6. 설계 결정과 이유

| 결정 | 이유 |
|---|---|
| 디렉터리 = 담당자 단위 | 한 사람이 자기 폴더만 수정해 파일 충돌과 책임 논쟁을 없앤다. |
| `schema.py`에 모듈 간 계약을 한 곳에 정의 | 구현 전에도 서로의 입출력을 가정하고 병렬 개발한다. |
| B/C는 `NotImplementedError` 스켈레톤, A는 구현됨 | 기존 공개 타입 유지. A DB·pending 설계와 B 소비자 제안은 [운영 계약](docs/INGEST_A.md)에 기록. |
| 증분 수집(`store.upsert_services`가 변경분 반환), 종료 서비스는 색인에서 제거 | 기획서 7장: 정기 수집 후 변경분만 재색인. |
| 구조 기반 분할 + 서비스명 접두 | 선정기준이 길어 한 덩어리로 임베딩하면 핵심 조건이 희석되고, 접두가 없으면 어느 제도의 조각인지 잃는다. |
| 소프트 필터 + `relax()` | 값이 비어 있으면 제외하지 않고, 결과가 없으면 필터를 풀어 재검색한다. |
| `retrieve/baseline.py`, `search(mode=...)` | Baseline → Vector → Hybrid → Hybrid+필터 비교 실험으로 하이브리드 효과를 검증한다. `mode` 기본값이 서비스 경로라 시그니처는 호환된다. |
| `evidence.py`(근거 선별), `Answer`에 `no_evidence` | 질문당 제도를 3~5개로 제한하고, 근거 없음 응답을 UI·평가가 구분해 쓴다. |
| `app/`은 `answer_question()` 하나만 호출 | UI 교체와 백엔드 수정이 서로 영향을 주지 않는다. |
| 평가 채점 단위는 `service_id` | 분할 방식이 바뀌어도 질문 세트를 재사용한다. |

검색 난점별 대응 위치(기획서 7.1)와 결정별 상세 근거는 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)를 봅니다.

## 7. 평가

- 검색: Hit Rate@K, Recall@K, MRR. 잠정 목표 Top-5 기준 Hit Rate 0.80, Recall 0.70, MRR 0.60 (`eval/metrics.TARGETS`)
- 답변: Answer Correctness, Faithfulness, Citation Accuracy
- 실험 순서: Baseline(SQL+키워드) → Vector → BM25 → Hybrid → Hybrid+정형 필터 → 최종. 결과는 `eval/results/`에 저장하고 요약을 기획서 9.4에 기록합니다. Top-K는 3 / 5 / 10을 비교합니다.
- 질문 세트: `eval/questions.jsonl` (30개 내외 목표, **청년 상황형 질문 중심** — 1.1 참고). 필드는 `id, category, question, relevant_service_ids, expected_answer, expect_no_answer`입니다. 정답 서비스는 수집 후 팀원이 지정하며, `todo`가 있는 항목은 수집 데이터에 있는지 확인 후 수정합니다.

## 8. 시작하기

```bash
pip install -r requirements.txt        # 설치
cp .env.example .env                   # 키 입력: GOV24_API_KEY(Decoding 키), OPENAI_API_KEY
python -m src.ingest.pipeline          # A: 실제 수집, 기본 증분 (라이브 실행 전 승인)
python -m src.ingest.pipeline --full   # A: 활성 조각 재생성, DB·이력·pending 보존
python -m pytest tests/ingest -q -ra   # A: 네트워크·키 없이 검증
python -m src.index.vector_store       # B: 색인 (진입점은 B가 확정)
python -m app.main                     # C: 앱 실행
python -m pytest -rs                   # 테스트
python -m eval.run_eval --experiment hybrid_filter --k 5   # 평가 (baseline|vector|bm25|hybrid|hybrid_filter)
```

Windows PowerShell에서 환경변수를 직접 설정한다면 `export` 대신 `$env:GOV24_API_KEY="키"`를 씁니다.

### 테스트

앞 담당자의 구현이 끝나기 전에도 뒤 담당자가 자기 코드를 시험할 수 있도록, 인계 지점마다 `tests/fixtures/`에 **가상 샘플 데이터**를 두었습니다. 실제 제도 내용이 아니며, 실제 출력이 나오면 일부를 교체합니다.

| 누가 시험하나 | 앞 단계가 없을 때 대신 쓰는 것 | 샘플 파일 |
|---|---|---|
| B (A 없이) | 가짜 조각 `Chunk` 48개 | `sample_chunks.json`, `sample_queries.json` |
| C (B 없이) | 가짜 검색 결과 + 가짜 조건 추출 | `sample_search_results.json`, `fake_search`·`fake_conditions` 픽스처 |

공용 훅은 B/C 구현 전(`NotImplementedError`) 테스트를 skip으로 표시합니다. A 전용 `tests/ingest/`는 미구현을 실패로 처리하며 HTTP·SQLite를 격리합니다. 전체는 `python -m pytest -rs`, A는 `python -m pytest tests/ingest -q -ra`로 실행합니다. A 합성 응답은 `tests/fixtures/a_synthetic_api.json`이고 기존 B/C fixtures는 보존합니다.

#### B가 A의 구현 없이 테스트하기

A의 구현이 끝나기 전에도 B가 자기 코드를 검증할 수 있도록 가상 샘플 조각(12개 제도 × 4항목 = 48개)과 샘플 질의를 두었습니다.

- 지역만 다른 중복 제도, 기한 지난 제도, 값이 빈 제도, 제도명 정확 일치 대상이 섞여 있습니다.
- 시험 대상: `filters`(소프트 필터·기한 제외·완화), `metrics`(지표 계산), `vector_store`·`hybrid`(색인 후 검색 기대값). 테스트 파일은 `test_filters.py`, `test_metrics.py`, `test_retrieval.py`입니다.
- `test_retrieval.py`의 색인 저장 경로 분리는 B가 결정합니다.

#### C가 B의 구현 없이 테스트하기

B의 구현(색인·검색)이 끝나기 전에도 C가 자기 코드를 검증할 수 있도록, **B가 C에게 줄 검색 결과**를 미리 정해 둔 샘플(`sample_search_results.json`)을 두었습니다. 2.2의 (3) B → C 인계 데이터와 같은 모양입니다.

| 사례 | 질문 | B가 줬다고 가정하는 결과 | 확인하려는 것 |
|---|---|---|---|
| `sr1` | "서울에 사는 27살 취업준비생인데 월세가 부담돼요…" | 조각 5개(서비스당 1개, 관련도 순). 그중 `SAMPLE011`은 지역·연령 값이 비어 있음 | 근거 선별, 인용, 값이 빈 제도의 "확인 필요" 표시, 면책 문구 |
| `sr2` | "내년에 새로 생길 예정인 청년 지원금은…" | 빈 리스트 | 근거 없음 → 추측하지 않고 모른다고 답함 (`no_evidence=True`) |

C가 쓰는 도구는 두 가지입니다 (`tests/conftest.py`).

```python
def test_예시(fake_search, fake_conditions):
    case = fake_search("sr1")          # hybrid.search를 sr1의 가짜 결과로 바꾼다 (B 없이)
    fake_conditions(case)              # extract_conditions도 고정한다 (LLM 없이)
    answer = generator.answer_question(case["question"])
    assert case["calls"][0]["filters"]["region"] == "서울"   # C가 B에게 넘긴 필터를 확인할 수 있다
```

- **시험 대상**: `condition_extractor.to_filters`, `evidence.select_evidence`(3~5개 제도로 제한), `citation.build_citations`(서비스명·소관기관·근거 문구·링크), `generator`(근거 없음 응답, 전체 흐름). 테스트 파일은 `test_generate.py`입니다.
- **LLM 없이 되는 것**: 조건→필터 변환, 근거 선별, 인용, 근거 없음 응답(LLM을 부르지 않는 경로). **LLM이 필요한 것**: 실제 답변 생성은 `OPENAI_API_KEY`가 없으면 skip됩니다.
- **지켜야 할 코드 규칙**: 가짜로 바꿔 끼울 수 있도록, C는 다른 모듈의 함수를 `hybrid.search(...)`, `condition_extractor.extract_conditions(...)`처럼 **모듈 속성으로 호출**합니다. `from src.retrieve.hybrid import search`처럼 이름만 가져오면 가짜가 적용되지 않습니다.
- B의 실제 결과가 나오면 `sample_search_results.json`의 `result_chunk_ids`를 실제 검색 결과로 교체하고, B가 보장하는 규칙(서비스당 1개, 최대 5개, 기한 지난 제도 없음)이 지켜지는지 함께 확인합니다.

## 9. 작업 규칙

- 자기 담당 디렉터리 밖의 파일을 고칠 때는 담당자와 먼저 합의합니다.
- 변경 전에 `docs/사전기획서.docx`의 해당 절을 확인하고, 설계가 바뀌면 기획서(8.3)에도 기록합니다.
- 새 라이브러리를 추가하면 `requirements.txt`를 함께 갱신합니다.
- **문서 동기화**: 디렉터리·모듈·함수 시그니처·데이터 계약·담당 영역·명령을 바꾸면 같은 커밋에서 `README.md`, `docs/ARCHITECTURE.md`, `CLAUDE.md`의 해당 부분을 함께 갱신합니다.

## 10. 구현 현황

| 모듈 | 상태 |
|---|---|
| `src/ingest/` | 수집·정제·SQLite·구조 분할·증분 pending·CLI 구현. 실제 API 세부 계약/JA 코드 의미는 미검증, 자동 소실 차단 |
| `src/common/config.py`, `src/common/schema.py`, `eval/run_eval.load_questions` | 구현됨 |
| `tests/ingest/` | A 전용 mock HTTP·임시 DB·가짜 소비자·CLI 테스트. 미구현은 실패 처리 |
| 기존 B/C 테스트 | 구현 전(또는 OPENAI_API_KEY 없음) skip. A 테스트와 구분해 해석 |
| 그 외 B/C | 스켈레톤 (`NotImplementedError`) |

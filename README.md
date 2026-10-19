# 개인 맞춤형 정부 지원제도 탐색 및 안내 서비스 (RAG)

사용자가 생활 상황을 자연어로 질문하면(예: "서울에 사는 27살 취업준비생이 받을 수 있는 청년 지원 제도"), 공공데이터에서 해당 가능성이 있는 정부 지원제도를 찾아 **자격조건·신청 절차를 출처와 함께** 안내하는 RAG 서비스입니다.

- 설계 근거: [`docs/사전기획서.docx`](docs/사전기획서.docx) (1~9장)
- 상세 설계 이유·변경 이력: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- AI 작업 지침(규칙 원문): [`CLAUDE.md`](CLAUDE.md)

> 현재는 **스켈레톤 단계**입니다. API 수집(`src/ingest/client.py`)과 공용 설정·데이터 타입만 구현되어 있고, 나머지는 `NotImplementedError`로 남겨 둔 인터페이스입니다.

## 1. 데이터

- 출처: 공공데이터포털 "행정안전부_대한민국 공공서비스(혜택) 정보"(보조금24) REST API, 약 1만 건
- 서비스 1건 = 서비스명, 지원대상, 선정기준, 지원내용, 신청방법, 신청기한, 소관기관, 상세 링크 + 지원조건(정형)
- 원본 응답은 `data/raw/`에 저장하고 **커밋하지 않습니다**. API 키는 `.env`에만 두고 **커밋하지 않습니다**.
- 첨부 문서·이미지는 수집하지 않고 상세 링크로 대신합니다.

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

수집 → 정제 → DB 적재 순으로 진행 로그가 나오고, 신규·변경된 서비스만 다음 단계로 넘어갑니다.

```
[client] serviceList 수집 완료: 10,213건 (perPage=500, 21페이지)
[client] supportConditions 수집 완료: 10,198건
[client] 원본 저장: data/raw/serviceList.json, supportConditions.json
[store]  upsert: 신규 120 / 변경 35 / 변동 없음 10,058
[store]  종료 처리: 8건 (색인에서 제거 대상)
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

- DB 컬럼명은 예시이며 A가 확정합니다(`schema.py`에 없는 부분). 이후 `Chunk.metadata`는 여기서 만들어집니다.
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

전체 규모는 서비스 약 1만 건 → 조각 수만 개로 예상하며, 수집 후 실제 수치를 기록합니다.

#### ③ 임베딩 — `embedder.embed_texts(texts)`

텍스트 N개를 넣으면 같은 순서로 벡터 N개가 나옵니다. bge-m3 기준 벡터 차원은 1024입니다.

```
입력: ["청년월세 지원(서울)\n서울에 거주하며 ...", "청년월세 지원(서울)\n부모와 따로 ..."]
출력: [[0.012, -0.034, 0.101, ... (1024개)], [0.008, -0.029, 0.095, ...]]   # list[list[float]]
```

#### ④ 벡터 저장소 — `vector_store.build_index(chunks)` / 조회

색인은 조각을 `chunk_id` 기준으로 upsert하고, 조회는 `(Chunk, 점수)` 목록을 돌려줍니다.

```
[index] upsert 조각 480개 (신규·변경 서비스분) / 제거 32개 (종료 서비스분)
[index] 총 조각 수: 41,276

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
[retrieve] 필터 적용: 후보 조각 41,276 → 6,310 (기한 지난 제도 제외, 값이 빈 항목은 통과)
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

## 3. 디렉터리 구조와 담당

```
src/
  common/    config.py, schema.py                                  # 전원 공용: 설정, 모듈 간 데이터 계약
  ingest/    client, store, chunker, pipeline                      # A
  index/     embedder, vector_store                                # B
  retrieve/  hybrid, filters, baseline                             # B
  generate/  condition_extractor, evidence, generator, citation    # C
app/         main.py                                               # C: UI
eval/        metrics, answer_eval, run_eval, questions.jsonl, results/
tests/       fixtures/(가상 샘플 조각·질의), conftest.py, test_*.py
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
| 함수는 시그니처·docstring만, 본문은 `NotImplementedError` | 인터페이스만 합의하고 구현은 담당자가 한다. 합의 전인 DB 스키마·색인 진입점은 확정하지 않는다. |
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
- 질문 세트: `eval/questions.jsonl` (30개 내외 목표). 필드는 `id, category, question, relevant_service_ids, expected_answer, expect_no_answer`입니다. 정답 서비스는 수집 후 팀원이 지정하며, `todo`가 있는 항목은 수집 데이터에 있는지 확인 후 수정합니다.

## 8. 시작하기

```bash
pip install -r requirements.txt        # 설치
cp .env.example .env                   # 키 입력: GOV24_API_KEY(Decoding 키), OPENAI_API_KEY
python -m src.ingest.pipeline          # A: 수집·정제·분할 (기본 증분, --full은 전체)
python -m src.index.vector_store       # B: 색인 (진입점은 B가 확정)
python -m app.main                     # C: 앱 실행
python -m pytest -rs                   # 테스트
python -m eval.run_eval --experiment hybrid_filter --k 5   # 평가 (baseline|vector|bm25|hybrid|hybrid_filter)
```

Windows PowerShell에서 환경변수를 직접 설정한다면 `export` 대신 `$env:GOV24_API_KEY="키"`를 씁니다.

### 테스트

A의 구현이 끝나기 전에도 B가 자기 코드를 검증할 수 있도록 `tests/fixtures/`에 가상 샘플 조각(12개 제도 × 4항목 = 48개)과 샘플 질의를 두었습니다.

- 지역만 다른 중복 제도, 기한 지난 제도, 값이 빈 제도, 제도명 정확 일치 대상이 섞여 있습니다. 실제 제도 내용이 아니며, A의 실제 조각이 나오면 일부를 교체합니다.
- 구현 전(`NotImplementedError`) 테스트는 실패가 아니라 skip으로 표시되고, 구현하면 같은 테스트가 자동으로 실제 검증을 시작합니다.
- `test_retrieval.py`의 색인 저장 경로 분리는 B가 결정합니다.

## 9. 작업 규칙

- 자기 담당 디렉터리 밖의 파일을 고칠 때는 담당자와 먼저 합의합니다.
- 변경 전에 `docs/사전기획서.docx`의 해당 절을 확인하고, 설계가 바뀌면 기획서(8.3)에도 기록합니다.
- 새 라이브러리를 추가하면 `requirements.txt`를 함께 갱신합니다.
- **문서 동기화**: 디렉터리·모듈·함수 시그니처·데이터 계약·담당 영역·명령을 바꾸면 같은 커밋에서 `README.md`, `docs/ARCHITECTURE.md`, `CLAUDE.md`의 해당 부분을 함께 갱신합니다.

## 10. 구현 현황

| 모듈 | 상태 |
|---|---|
| `src/ingest/client.py` | 구현됨 (API 호출·페이지 순회·원본 저장). 실제 응답 필드는 검증 전 |
| `src/common/config.py`, `src/common/schema.py`, `eval/run_eval.load_questions` | 구현됨 |
| `tests/` | 샘플 데이터 검증 3개 통과, 나머지 16개는 구현 전이라 skip |
| 그 외 | 스켈레톤 (`NotImplementedError`) |

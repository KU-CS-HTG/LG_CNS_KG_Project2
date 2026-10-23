# 코드 구조와 설계 이유

> 코드 구조가 바뀌면 이 문서도 같은 커밋에서 갱신한다 (`CLAUDE.md` '문서 동기화').
> 설계 근거는 [`사전기획서.docx`](사전기획서.docx)(이하 기획서), 프로젝트 규칙은 `CLAUDE.md`.

## 1. 전체 구조 — 색인과 질의응답

![RAG 전체 구조](images/rag-structure.png)

색인은 문서(데이터)가 바뀔 때만 다시 돌리고, 질문은 아래 온라인 경로만 돈다. 구조도의 단계(①~⑥)와 코드의 대응:

| 단계 | 구조도 | 코드 | 담당 |
|---|---|---|---|
| 오프라인 | 문서(PDF) → ① 로드 | `src/ingest/client.py`, `store.py` (현재 검색 데이터는 보조금24 API → DB; A-1 원본 카탈로그는 별도) | A |
| | ② 분할 | `src/ingest/chunker.py` | A |
| | ③ 임베딩 | `src/index/embedder.py` | B |
| | ④ 벡터 저장소 | `src/index/vector_store.py` | B |
| 온라인 | (질문 → 조건 추출) | `src/generate/condition_extractor.py` — 구조도에는 없고 기획서 8.1에서 추가 | C |
| | ⑤ 검색 Top-K | `src/retrieve/hybrid.py`, `filters.py` | B |
| | (근거 선별) | `src/generate/evidence.py` | C |
| | ⑥ 생성 LLM → 답변 + 출처 | `src/generate/generator.py`, `citation.py` | C |

구조도 대비 달라진 점: ① 현재 검색 입력은 API이고 정형 지원조건이 함께 들어온다 / 검색 앞에 조건 추출과 정형 필터가 붙는다 / 검색과 생성 사이에 근거 선별이 있다. A-1 공식 원본 카탈로그는 아직 검색 경로에 연결하지 않는다.

## 2. 디렉터리 구조

```
src/
  common/    config.py, schema.py                       # 전원 공용: 설정, 모듈 간 데이터 계약
  ingest/    client, normalize, merge, storage, store, chunker, pipeline, locking, handoff* # A
  index/     embedder, embed_cache, embed_shard, vector_store  # B: ③ 임베딩(분담 계산 포함), ④ 벡터 저장소
  retrieve/  hybrid, filters, baseline                  # B: ⑤ 검색, 정형 필터, 비교 실험용 Baseline
  generate/  condition_extractor, evidence, generator, citation   # C
app/         main.py                                    # C: UI
eval/        metrics, answer_eval, run_eval, label_helper, questions.jsonl, results/
tests/       pytest. fixtures/(가상 샘플 조각·질의·검색 결과), conftest.py(fake_search 등), test_*.py
data/raw/    원본 응답 (커밋 금지)
data/official_documents/  A-1 isolated catalogs/originals/diagnostics (커밋 금지)
docs/        사전기획서.docx, ARCHITECTURE.md, images/
```

## 3. 왜 이렇게 나눴나

| 결정 | 이유 (기획서 근거) |
|---|---|
| 디렉터리 = 담당자 단위 (A `ingest` / B `index`+`retrieve`+`eval` / C `generate`+`app`) | `CLAUDE.md` 담당 영역과 1:1. 한 사람이 자기 폴더만 수정해 파일 충돌과 책임 논쟁을 없앤다. |
| `src/common/schema.py`에 `Chunk`·`Metadata`·`Filters`·`Recommendation`·`Answer`를 한 곳에 정의 | 모듈 간 계약(A→B 조각, C→B 필터, B→C 검색 결과, C→UI 답변)을 코드로 고정해 구현 전에도 병렬 개발한다. 바꿀 때는 이 파일과 `CLAUDE.md` 인터페이스 절부터 고친다. |
| A/B/C implementations retain existing public contracts | A-owned stored-event consumer connects the existing Chroma/BM25 APIs without changing B/C or shared types/configuration. |
| `ingest`를 `client` / `store` / `chunker` / `pipeline`으로 분리 | ①로드와 ②분할에 대응. 수집은 API 의존, 분할은 순수 함수라 따로 테스트한다. DB는 정형 필터·Baseline 실험·변경 감지의 원천이다 (7장). |
| `store.upsert_services`가 변경된 활성 `service_id`를 반환, `pipeline.run`은 미확인 증분을 재전달 | 일일 증분 수집. A가 서비스 전체 교체/제거를 durable pending에 기록한다. 무인자 `mark_closed()`는 안전한 no-op. 검증된 전체 목록만 내부 소실 판정을 허가하며 현재 실 API 미검증으로 차단된다. The implemented `ingest.handoff` consumer verifies and acknowledges stored snapshots through B APIs. |
| `chunker`: 구조 기반(4개 항목) + 긴 항목은 Recursive, 서비스명 접두 | 8.1 문서 분할. 선정기준이 길어 한 덩어리로 임베딩하면 핵심 조건이 희석되고(7.1), 접두가 없으면 어느 제도의 조각인지 잃는다. |
| `Metadata`에 지역·소관기관·분야·신청기한·연령 범위·대상 태그 | 8.1 메타데이터. 종료된 제도와 지역·연령이 안 맞는 후보를 제거하고, 지역만 다른 중복 제도(7.1)를 구분한다. |
| `index`(오프라인)와 `retrieve`(온라인) 분리 | 구조도의 오프라인 색인 / 온라인 질의응답 경계를 코드에 반영. 색인은 문서가 바뀔 때만 실행. |
| `retrieve/filters.py` 별도, 소프트 필터 + `relax()` | 8.1: 값이 비어 있으면 제외하지 않고, 결과가 없으면 필터를 풀어 재검색. 평가에서 "Hybrid"와 "Hybrid+정형 필터"를 따로 비교해야 한다(9.4). |
| `retrieve/baseline.py`(SQL+키워드), `search(mode=...)` | 6.2·9.4: Baseline → Vector → Hybrid → Hybrid+필터 비교 실험으로 하이브리드의 효과를 검증한다. `mode` 기본값은 서비스 경로인 `hybrid_filter`라 B→C 시그니처는 호환된다. |
| `generate/evidence.py` (근거 선별) | 구조도·`CLAUDE.md` 흐름의 '근거 선별'. 조각을 서비스 단위로 묶어 질문당 3~5개 제도로 제한하고(5.1), 불확실한 제도를 '확인 필요'로 표시할 재료를 만든다. |
| `Answer = text + Recommendation[] + no_evidence` | 5.1 최종 경험: 제도마다 해당 가능 이유·지원내용·신청방법·기한·확인 필요 조건·근거 원문·링크. 근거가 없으면 `no_evidence`로 구분해 평가(근거 없음 질문)와 UI가 쓴다. |
| `citation.py`에 `DISCLAIMER`, `NO_EVIDENCE_MESSAGE` 상수 | 6.1: 최종 자격은 담당 기관 확인 안내와 '모른다' 응답 문구를 한 곳에서 관리해 모든 답변에 일관되게 붙인다. |
| `app/`은 `answer_question()` 하나만 호출 | UI 교체와 백엔드 수정이 서로 영향을 주지 않게 한다. |
| `eval/` 별도 최상위, 채점 단위는 `service_id` | 검색 지표(B)·답변 지표(C)·질문 세트(전원)가 섞이는 공용 영역. 조각이 아니라 서비스 단위로 정답을 지정해 분할 방식이 바뀌어도 질문 세트를 재사용한다. |
| `eval/questions.jsonl` 필드: `id, category, question, relevant_service_ids, expected_answer, expect_no_answer` | 9.1: 상황형·제도명 아는 질문·지역·연령·기한 지난 제도·근거 없음을 섞어 30개 내외. 정답 서비스는 수집 후 팀원이 지정한다. 현재 4개는 기획서 예시이며 `todo`가 있는 항목은 수집 데이터에 있는지 확인 후 수정한다. |
| `eval/metrics.TARGETS` | 9.2 잠정 목표: Hit Rate@5 0.80, Recall@5 0.70, MRR 0.60. |
| `tests/fixtures/sample_chunks.json` (가상 조각 48개 = 12개 제도 x 4항목) | A가 끝나기 전에 B가 자기 코드를 검증하도록 `Chunk` 계약대로 만든 가짜 데이터. 지역만 다른 중복 제도, 기한 지난 제도, 값이 빈 제도, 제도명 정확 일치 대상을 일부러 섞었다. 실제 제도 내용이 아니며 A의 실제 조각이 나오면 일부를 교체한다. |
| `tests/fixtures/sample_queries.json` | 검색 기대값(`expect_top1`, `expect_in_top5`, `expect_not_in_results`)과 필터 `today`를 고정해 시간이 지나도 결과가 변하지 않게 한다. |
| `tests/conftest.py`: `NotImplementedError`를 skip으로 변환 | 스켈레톤 단계에서는 미구현 테스트가 실패로 보이지 않고, 구현하는 순간 같은 테스트가 실제 검증을 시작한다. |
| `tests/fixtures/sample_search_results.json` + `fake_search`·`fake_conditions` 픽스처 | B가 끝나기 전에 C가 자기 코드를 검증하도록 B→C 인계 데이터(Top-5 조각, 근거 없음 사례)를 미리 정해 두고 `hybrid.search`·`extract_conditions`를 가짜로 바꿔 끼운다. 이를 위해 C는 다른 모듈 함수를 모듈 속성으로 호출한다(`generator.py` 주석에 규칙 명시). |
| `test_generate` | C 코드(조건→필터, 근거 선별, 인용, 근거 없음 응답, 전체 흐름). LLM이 필요한 실제 생성 테스트는 `OPENAI_API_KEY`가 있을 때만 실행. |
| `embedder`: 모델 지연 로딩, L2 정규화, `kind`(passage/query)별 e5 접두 자동 처리, `embed_query`·`embedding_dim` 제공 | 모델 로딩은 수 초~수십 초 걸리므로 처음 쓸 때 한 번만 한다. 정규화하면 벡터 저장소가 내적/코사인 어느 쪽이든 같은 결과를 낸다. e5는 접두가 없으면 품질이 떨어지는데, 호출하는 쪽이 모델을 몰라도 되게 이 모듈이 처리한다(모델 교체 실험이 쉬움). 시그니처 `embed_texts(texts, kind="passage")`는 기존 호출과 호환된다. |
| 벡터 저장소: Chroma(정식 벡터 DB), 소프트 필터는 저장 시 대체값(지역 `""`, 연령 -1/999, 기한 99999999)으로 처리, `build_where` | 일반 개인·가구 지원으로 범위가 확대되어 정식 벡터 DB를 쓰기로 했다(규모가 작아 numpy+SQLite도 가능하나 확장성 우선). Chroma 필터에는 "값이 없으면 통과"가 없어서, 빈 값을 통과하는 값으로 미리 바꿔 저장하는 방식으로 기획서 8.1의 소프트 필터를 구현했다. 읽을 때는 원래대로(키 생략) 되돌린다. |
| BM25: `rank_bm25` + 글자 2-gram, 색인은 Chroma 텍스트에서 파생·버전 파일로 갱신 | Chroma에 BM25가 없다. 한국어는 조사 때문에 단어 단위가 불리하고 trigram은 두 글자 단어를 못 찾으므로 2-gram. 저장소가 둘로 나뉘지만 BM25 색인은 파생물이라 `index_version` 파일이 바뀌면 다시 만들어 동기화 문제를 피한다. |
| 임베딩 모델별 컬렉션(`chunks__<모델명>`) | 모델을 바꾸는 실험(bge-m3 ↔ e5)에서 차원 불일치로 저장소가 깨지는 것을 막는다. |
| `build_index`: 서비스별 옛 조각 중 이번에 없는 것을 삭제, `remove_services`, `clear_index`, `get_service_chunks` | 증분 색인(기획서 7장)과 종료 서비스 반영. `get_service_chunks` is used by C evidence expansion to retrieve all sections of a selected service. |
| 검색: RRF(순위 합산, 상수 60, 후보 50개) + 서비스당 조각 1개, `search_with_info`(점수·적용된 필터·완화 여부) | 코사인과 BM25는 점수 단위가 달라 순위로 합친다(튜닝 최소화). 같은 제도의 조각이 Top-K를 채우지 않게 서비스 단위로 줄인다(구조도 '중복 제거', 기획서 8.1). 평가(9.4)에서 `mode`별로 점수를 비교하고, 완화 여부는 C가 안내 문구에 쓸 수 있다. |
| 필터 완화 순서: 지역 → 연령, 신청기한은 풀지 않음. 분야·상황은 필터가 아니라 풀지 않음 | 연령은 자격과 직결되어 마지막에 푼다. 종료된 제도를 보여주지 않는 것은 기획서 규칙이라 완화 대상에서 뺀다. (분야는 처음에는 가장 먼저 푸는 필터였으나 아래 결정으로 필터에서 빠짐) |
| **관심 분야(`field`)는 필터가 아니라 가산점**: `build_where`·`apply_filters`에서 분야 조건 제거, `hybrid.boost_by_field`가 맞는 분야에 `FIELD_BOOST`(0.002)를 RRF 점수에 더함, `filters.field_matches`(포함 관계) | 분야는 자격이 아니라 관심사다. 화면 테스트에서 질문("월세가 부담되고 취업 준비 중")의 분야가 "주거" 하나로 뽑히자 구직 지원 제도가 필터로 통째로 걸러졌다. 또 LLM이 뽑는 분야 이름과 실제 데이터의 서비스분야 이름이 다르면(예: "주거" vs "주거·자립") 맞지 않는 제도가 모두 사라지고, 분야가 비어 있어 통과하는 제도만 남아 결과가 비지 않으므로 완화도 일어나지 않는 문제가 있다. 가산점은 거르지 않으므로 이름이 달라도 결과가 그대로 나오고 맞으면 앞으로 올라간다. B·C 합의(C는 `Filters`에 `field`를 그대로 보냄). 값은 평가(`run_eval`)로 조정하며 0이면 끈다. 검색어에 분야를 덧붙이는 방법은 복합 질문에서 한쪽으로 쏠릴 수 있어 쓰지 않았다. |
| `filters.apply_filters`(파이썬)와 `vector_store.build_where`(저장소) 규칙을 따로 두고 일치 테스트 | 저장소 안에서 거르는 것이 빠르지만, Baseline 결과나 후처리는 이미 꺼낸 목록을 거른다. 두 곳의 규칙이 어긋나지 않도록 샘플 데이터에서 결과가 같은지 매개변수 테스트로 확인한다. |
| `test_vector_store`, 테스트용 `fake_embedder`·`tmp_vectorstore`·`indexed_sample` 픽스처 | 임시 폴더와 글자 2-gram 해시 가짜 임베딩으로 실제 Chroma 동작(upsert·삭제·필터·BM25 갱신)을 모델 없이 검증. `test_retrieval`도 같은 픽스처를 쓴다. |
| `metrics`: 정답 없는 질문은 계산에서 제외, 평가할 질문이 없거나 질문 수가 안 맞거나 k<1이면 ValueError, 같은 id가 중복되면 처음 자리만 셈, `no_evidence_rate`·`evaluate`·`check_targets` 제공 | 정답이 없는 질문(근거 없음)은 Hit Rate·Recall·MRR이 정의되지 않으므로 분모에서 빼야 지표가 왜곡되지 않는다. 조용히 0을 돌려주면 잘못된 입력(질문·정답 개수 불일치)을 못 잡으므로 오류로 알린다. 근거 없음 질문이 관련 없는 제도를 내보내지 않는지는 `no_evidence_rate`로 따로 보고, 관련도 하한선을 정할 때 쓴다. `evaluate`는 K=3·5·10을 한 번에 계산해 `run_eval`이 쓰기 쉽다. |
| `run_eval`: 검색 함수를 주입(`SearchFn`)하고 `run_experiment`·`format_report`·`comparison_table`·`save_result`로 분리 | 집계·출력·저장을 검색 구현과 떼어 놓아 가짜 검색 함수로 계산을 검증할 수 있다(실제 색인·모델 없이). `main`은 이를 묶는 얇은 껍데기. |
| 질문에 `filters`(정답 조건)를 직접 적음 | 검색 평가에 LLM 조건 추출의 오류가 섞이지 않게 한다(조건 추출은 C가 따로 평가). `hybrid_filter` 실험에서만 쓴다. |
| 질문 세 종류(평가 대상 / 근거 없음 / 정답 미지정)와 정답이 하나도 없을 때의 오류 | 정답 미지정 질문이 지표를 조용히 낮추지 않게 제외하고 개수를 경고한다. 정답이 전혀 없으면 0점이 아니라 "정답을 채우세요"라고 알려 준다. |
| 질문마다 가장 큰 K(10)만큼 가져와 K=3·5·10을 한 번에 계산 | 실험마다 검색을 세 번 돌리지 않는다. `--k`는 표에서 강조할 K. |
| `--today` 옵션, 결과 JSON에 설정(모델·RRF·기준일) 저장 | 신청기한 때문에 날짜가 바뀌면 결과가 달라지므로 기준일을 고정해 재현하고, 나중에 실험을 비교할 때 어떤 설정이었는지 남긴다. |
| 놓친 질문 목록(정답 순위·실제 검색 결과)과 범주별 표 | 점수만으로는 원인을 알 수 없다. 약한 질문 유형과 실패 사례를 바로 보여 분할·필터·임베딩·BM25 중 무엇을 손볼지 정하는 근거가 된다. |
| `--experiment all`이 기획서 9.4용 마크다운 표를 출력, 구현 전 baseline은 건너뜀 | 비교 실험 결과를 9.4에 그대로 옮겨 적도록 한다. |
| `test_run_eval`(질문 세트 파일 형식 점검 포함) | 가짜 검색으로 집계를, 샘플 색인으로 `main` 전체 흐름을 검증한다. 팀이 `questions.jsonl`을 고친 뒤 id 중복·필드 누락·근거 없음 질문의 정답 여부가 깨지지 않았는지도 확인한다. |
| `eval/label_helper.py` (질문 정답 채우기 도구)를 `eval/`에 둠, 사용법은 `docs/LABELING.md` | 정답은 A의 파이프라인이 끝나기 전에도, 검색 시스템과 독립적으로 정해야 한다(순환 평가 방지). 그래서 보조금24 API에서 서비스 목록을 직접 받아 키워드로 찾는 별도 도구로 만들었다. `eval/questions.jsonl`을 고치는 도구라 평가(`eval/`)에 둔다. 팀원 전원이 쓰므로 사용법과 기준을 문서로 남긴다. |
| 정답 기준: 질문당 5개 이내 핵심 제도, 질문에 없는 자격 조건은 따지지 않음, 기한 지난 제도 제외, **지역 없는 질문은 전국 단위 제도** | Recall@K의 분모가 정답 수라 정답이 많으면 목표를 달성할 수 없다. 자격(소득 등)은 답변 단계의 "확인 필요"로 처리되므로 검색 정답에서는 가능성만 본다. 실제 서비스는 지역을 추가 질문으로 받으므로 지역 없는 첫 질문의 정답은 전국 제도로 하고, 지역 제도는 `followup`으로 만든 별도 문항(`filters.region`, `follow_up_of`)에서 평가한다. |
| 도구가 전국/지역(소관기관), 기한 지남(신청기한 문자열)을 추정해 보여 주고, 저장 시 지역·기한이 안 맞으면 경고 | 약 1만 건에서 기준에 맞는 제도를 사람이 고르기 쉽게 하되, 두 값은 자유 형식 문자열에서 추정하는 것이라 판정을 강제하지 않고 경고로만 다룬다. `check`는 id·이름 오류(종료 코드 1)와 경고를 구분한다. |
| `test_embedder` | 실제 모델 없이 가짜 모델로 순서·정규화·배치·접두를 검증. 의미 검증(월세 질문이 월세 조각에 더 가까운가)은 `RUN_REAL_EMBEDDING=1`일 때만 실제 모델로 실행. |
| `test_filters`, `test_metrics`, `test_retrieval`, `test_fixtures` | B 코드(필터 규칙, 지표 계산, 색인·검색 종단)와 샘플 데이터가 데이터 계약을 지키는지 확인. 필터·지표 테스트는 참조 구현으로 통과를 확인했다. |
| `data/raw/`, `.env`는 `.gitignore` | 원본 응답과 API 키는 커밋 금지. `.env.example`로 필요한 키만 공유. |

### 3.0 기획서 8.3 기록용 (벡터 저장소)

| 선택/변경 항목 | 발견한 문제 | 선택 이유 | 결과 |
|---|---|---|---|
| 벡터 저장소 = Chroma | 수천~수만 조각이라 어떤 방식도 가능하나 general personal/household support included | 확장성과 정식 벡터 DB 사용 | (수집 후 규모·검색 성능 기록) |
| 소프트 필터를 저장 시 대체값으로 처리 | Chroma 필터는 값이 없는 항목을 통과시키는 조건을 표현하기 어려움 | 빈 값을 통과하는 값으로 저장해 where 조건만으로 처리 | 테스트에서 연령·지역·기한·분야 규칙 확인 |
| BM25 = rank_bm25 + 글자 2-gram | Chroma에 BM25가 없고, 한국어는 조사·두 글자 단어 문제 | 추가 설치 없이 한국어 기본 대응, 필요 시 형태소 분석기로 교체 | Baseline 실험에서 비교 |

기획서 8.3 기록용 (⑤ 검색): 선택/변경 항목 "Hybrid 결합 방식 = RRF(상수 60, 후보 50개), 서비스 단위 중복 제거", 발견한 문제 "코사인·BM25 점수 단위가 달라 직접 합치기 어렵고, 조각 단위 Top-K는 같은 제도로 채워짐", 선택 이유 "순위만 써서 튜닝 최소화, 서비스당 1개로 제한", 결과 "(실제 데이터에서 Vector 단독·BM25 단독과 Hit Rate/MRR 비교 기록)".

### 3.1 Dataset scope — young users, personal/household benefits

The audience is young users (청년 사용자). Indexed programs include general individual
and household support: employment, housing, education, income/living, marriage,
pregnancy, childbirth, parenting and care, including benefits through children or dependents.

`ingest.policy.inclusion_decision()` centralizes `personal-household-v1`: `include`
with evidence, `provisional` when applicability is uncertain, or `exclude` for explicit
institution-only recipient statements. Absence of youth keywords, older/dependent
ages, or business wording alone never establishes exclusion. Ambiguous applicability
is provisionally included. Youth keywords/verified applicant-age overlap remain
explanatory tags; `is_youth_service()` retains its legacy boolean tagging behavior.

Dataset inclusion is separate from eligibility. No child age becomes applicant age,
no undocumented JA code is decoded, and A does not infer residence from agency names.
B's index-time region fallback remains a retrieval heuristic. Unknown is not unrestricted.
Excluded institutional records retain minimal audit; historical `youth_scope_exit`
records need usable original sections before they can become searchable again.

See [raw reprocessing and commands](INGEST_A.md#historical-raw-reprocessing-원본-재처리).
`pipeline --full` still collects APIs and requeues active chunks; it cannot recover
discarded sections by itself. No evaluation labels are read by the classifier or changed.

## 4. 데이터 흐름

오프라인(문서 변경 시, 기본 1일 1회 증분): `ingest.client` → `ingest.store`(정제·DB·변경 감지) → `ingest.chunker`(`Chunk`) → `index.embedder` → `index.vector_store`

온라인(질문마다): 질문 → `condition_extractor`(조건 → `Filters`) → `retrieve.hybrid.search`(필터 + Vector + BM25, 중복 제거, 필요 시 필터 완화) → `evidence.select_evidence` → `generator.generate_answer` + `citation` → `Answer`

## 5. 기획서 7.1 검색 난점의 대응 위치

| 난점 | 대응 코드 |
|---|---|
| 길고 복잡한 선정기준 | `chunker` 항목 분할 + Recursive |
| 지역만 다른 중복 제도 | `Metadata.region` 필터, `hybrid.dedupe` (MMR은 문제 관찰 시) |
| 제도명·기관명·지역명 정확 일치 | `vector_store.bm25_search` |
| 종료된 사업 혼재 | `Metadata.deadline`, `filters.is_expired`, `store.mark_closed` |
| 수치 조건(중위소득 n% 등) 비교 약함 | 정형 필터 + `Recommendation.needs_check`('확인 필요') |
| 표·첨부·이미지 | 수집 제외, 상세 링크로 대체 (`client`) |

## 6. 구현 현황

| 모듈 | 상태 |
|---|---|
| `ingest/client.py` | timeout·재시도·원본/완전성 구현. OAS·최초 3건씩의 필드/페이지 관측, 전체 종료·일관성은 미검증 |
| `ingest/normalize.py`, `store.py`, `chunker.py`, `pipeline.py` | 결정적 정제·개인/가구 포함 정책·SQLite·트랜잭션 pending·구조 분할·CLI 구현. 상세 운영 계약은 [INGEST_A.md](INGEST_A.md) |
| `common/config.py`, `common/schema.py`, `eval/run_eval.load_questions` | 구현됨 |
| `tests/ingest/` | A 전용 합성 HTTP·임시 DB·가짜 소비자·CLI 검증. A NotImplementedError는 실패 처리, 공용 훅/fixtures 보존 |
| Existing B/C tests | Implemented offline regression coverage; the opt-in actual-model test is skipped by default. This is not search/answer quality evaluation. |
| `ingest/handoff*.py`, `locking.py` | Stored snapshot → B apply → persistent/Vector/BM25 verification → exact ack; real Chroma/fake embeddings, subprocess locks and C evidence expansion tested. |

### 6.1 A 구현과 팀 결정 경계

SQLite tables (services, ingestion_runs, pending_changes, small ingest_state cursor/identity table)로 상태와 인계를 보존한다. 서비스명·본문·기관·URL·기간·metadata·검증 자격을 해시하며 운영 시각을 제외한다. Active data follows personal-household-v1; expiry, disappearance and policy_scope_exit remain separate. Legacy youth_scope_exit records remain recoverable from raw. 성공 상세의 빈 값은 삭제, 실패/미관측은 이전 정상 관측 보존이다. `normalize.py`는 미확인 JA* 코드·본문의 자녀 나이·기관에서의 지역을 추정하지 않는다.

`--full`은 DB 초기화 없이 활성 전체 조각을 pending에 재생성한다. 기본 `run()`도 이전 미확인 조각을 반환할 수 있다. 읽기·반환은 확인 응답이 아니다. [A 운영 계약](INGEST_A.md)에 필드 매핑, 확인 수준, 조건 어댑터, 스케줄, 실패 복구 및 B API를 기록했다.

B already removes stale chunk IDs for each nonempty service replacement. The consumer reuses that implementation and calls `remove_services()` explicitly for removals/empty replacements. Verification precedes exact service_id+version+event_id acknowledgment. Optional region codec/filter/reader wiring is documented in §6.4. Actual age-code mapping and complete residence-code semantics remain deferred; narrowly scoped residence-text evidence is implemented. Missing agency/URL display remains a separate decision. The personal/household inclusion expansion is agreed; uncertain applicability is provisional.

### 6.2 A 2차 수정

`merge.py`는 공통 필드의 현재 관측·성공 빈 값·키 누락·과거 fallback을 구분한다. 상세 실패 시 현재
목록의 의미 있는 값이 과거 상세를 대체하고 목록에도 없는 상세 본문은 유지한다. 두 현재 원천 충돌은
목록 우선/기한 보수적 선택의 명시적 규칙으로 처리하고 `_field_provenance`, `_merge_conflicts`와
실행별 raw 감사 기록에 남긴다. 수집 시각·원천 수정일시로 최신성을 단정하지 않는다.

`storage.StoragePaths`는 모든 SQLite·잠금·raw·pending 접근에 명시적으로 전달한다. live 기본값을
유지하면서 fixture/validation은 독립 경로를 쓰며 운영 목적지·별칭·sidecar 중첩을 쓰기 전에 거부한다.
프로그램 fixture 경로도 CLI와 동일하게 격리한다. pytest는 별도 tmp_path를 사용한다.
HTMLParser는 td/th 셀 경계와 tr/블록 개행을 보존하고, 수정된 정규 본문은 다음 관측 때 한 번의 전체 교체를 유발할 수 있다.

라이브는 격리 DB에서 1페이지×3건×3엔드포인트만 sample로 확인했다. API 오류·HTML 표는 mock/합성 검증이다.
JA 기본 라벨을 공식 OAS에서 확인했으나 신청자/부양자·경계·특수값·지역 자격 어댑터는 보류한다.
B 소비자는 우선 하나를 권장하며 A stale-ack 보호가 B의 stale write까지 막지는 않는다. 기관/URL 품질은
`get_record()`로 조회하고 인용 후보 보류·표시 정책은 B/C 합의가 필요하다.

### 6.3 Stored-event consumption (증분 색인 인계)

`handoff.py` is the A-owned orchestration/inspection CLI; `handoff_worker.py` isolates B's process-global paths/model settings; `handoff_index.py` adapts existing B APIs. Source event rows contain complete stored snapshots (including a valid empty list), ordered by service ID. The consumer never reconstructs an older event from current service data or initiates ingestion.

The worker locks `<index-dir>.handoff.lock`, then `<db>.ingest.lock`, before reading/revalidating tokens and applying anything. Ingestion takes only the source lock; public `apply_run` and `upsert_services` participate, and the pipeline uses a private already-locked helper. Lock acquisition is nonblocking, with immediate failure. OS handle closure/process death releases leases; leftover files are harmless. The worker, rather than its launcher, owns the locks, so terminating the launcher cannot leave an unlocked index writer. Direct B APIs/legacy CLI and arbitrary SQL writes do not participate and must not run concurrently.

After model/index compatibility checks, both source and target store a matching association manifest: resolved DB/index paths, exact model ID, embedding dimension, max sequence length, cosine metric and B passage/query policy. One source acknowledgment queue supports one association. Existing nonempty collections need explicit `--adopt-existing`; model name collisions in legacy slugs cannot establish original model identity, so adoption requires operator knowledge. No collection reset/model download is used to resolve mismatches. Manifests are runtime state, preserved for replay and excluded from Git.

Verification compares exact service chunk IDs/text/B storage metadata and unchanged unrelated documents. Vector queries are scoped to the service and request its entire chunk set. BM25's rebuilt corpus must match all persistent IDs/token frequencies; real BM25 searches are checked against B's positive-score semantics. In tiny/common-term corpora Okapi may assign nonpositive scores: corpus membership proves visibility, and the consumer does not change ranking or demand an accidental positive top-K hit. Removal checks use persistent and BM25 corpus absence, not a top-K miss.

Chroma source documents persist; BM25 remains a derived cache invalidated by `index_version`. There is no SQLite/Chroma distributed transaction. A crash/verification/ack failure leaves replayable work; idempotent replacement/removal converges on retry. The first failure stops application with nonzero status; earlier verified/acknowledged events stay complete. Dry-run is read-only and performs no worker/model/collection initialization. Exact PowerShell commands, validation isolation and recovery are in [INGEST_A.md](INGEST_A.md#stored-event-consumer-증분-색인-인계-소비자).

Interest field remains a score boost; age/region/deadline semantics and C full-service evidence expansion are unchanged. Only fake embeddings are injected in integration tests. Actual-model, full API traversal and retrieval/answer quality evaluation are not established by this pass.

### 6.4 Metadata tracing and resumable raw replay

Region aliases now share a canonical vocabulary; optional `region_provenance` distinguishes
explicit residence evidence, agency inference, legacy values and unknown. Only narrowly scoped
지원대상/선정기준 residence statements are extracted. District/multiple-region evidence is kept
without claiming full eligibility; the existing unknown-region soft filter remains.
Korean full dates (`2026년9월9일`) and strict full-date ranges support expiry; ambiguous periods remain unknown.

Use `python -m src.ingest.metadata_tools trace ...` for read-only source→index diagnosis,
`correct ... --dry-run` before explicit selected-service `--apply`, and
`python -m src.ingest.reprocess ... --offset N --limit K --checkpoint NAME` / `--resume NAME`.
Exact tested PowerShell commands, checkpoint bindings, deployed-index findings and safe correction
limits are in [INGEST_A.md](INGEST_A.md#integrated-metadata-and-resumable-replay-2026-10-22).
Code changes do not repair old documents automatically. No production backfill or embedding reuse is implicit.

`Metadata.region_provenance` is optional and uses string storage, preserving old constructors.
`common/regions.py`, `ingest/regions.py`, `metadata_tools.py` own canonicalization, scoped extraction
and diagnostics/correction. Shared B codec/filters and minimal C provenance readers are updated
additively. Ingest-state UUID/cursor writes share the existing service/event/run transaction;
auxiliary-output failure cannot advance or rewind authoritative DB progress.

### 6.5 Official decision documents — A-1

`document_catalog`, `document_acquire`, `official_documents` and the small checked source
registry implement separate catalog/original/diagnostic paths. Logical document, exact-byte
published version, artifact, retrieval attempt and topic-specific service link are distinct.
Source observations/failures and auditable metadata corrections retain history. As-of inspection
keeps publication/finality separate from inclusive verified effective intervals, exposing
unknown dates, future rules, competing versions and scoped amendment review.

The existing API service replacement boundary would delete API chunks if document-only chunks
were submitted under the same service_id. Therefore A-1 does not change shared types, B/C or
the service queue, and does not write a document index. A separate collection, version-scoped
replacement, document queue/association, bounded related-clause expansion and DocumentCitation
are proposed for A-2/A-3 and still require team agreement. pypdf adds structure/page-count
validation only. Product direction, actual source evidence, limitations and detailed-condition
evaluation plan: [OFFICIAL_DOCUMENTS_A1.md](OFFICIAL_DOCUMENTS_A1.md).

## 7. 변경 이력

- 2026-10-23: A-1 isolated official document/source catalog, bounded originals, content-addressed versions, temporal inspection, auditable service/topic links and B/C proposal. No document extraction/index/routing changes or evaluation-label edits.

- 2026-10-20: Implement stored-event handoff consumer, exact verification/ack, OS synchronization and one DB/index/model association; document dry-run/application/replay and test real Chroma/BM25 plus C evidence expansion with fake embeddings. Record the decision in planning §8.3; preserve B/C/shared contracts.

- 2026-10-19 A 2차 수정: 현재 목록/과거 상세 충돌, fixture 저장소 격리, HTML 표 경계 보완. 회귀 테스트와 격리 라이브 3건씩의 표본 검증. 전체 API 적합성과 실제 연령/지역·B 색인 통합은 미완료. 기획서 8.3에 설계 변경 기록.

- 2026-10-19 A 구현: client 강화, normalize 추가, SQLite·실행 이력·pending 및 구조 분할·CLI 연결. A 전용 합성 fixture/테스트 추가. API 명세·실측 미검증 때문에 자동 소실 차단, JA* 자동 해석 보류. README·CLAUDE·이 문서·기획서 8.3 동기화. B/C 소유 코드·공용 스키마·설정·기존 fixtures는 보존.

- 초기 스켈레톤 작성. API 연결 테스트용 `youth_benefits.py` 삭제.
- 기획서·RAG 구조도 반영: `Metadata`/`Filters`/`Recommendation` 추가, `Answer` 구조 확장, 증분 수집·종료 처리(`store`, `pipeline`), Recursive 분할, `retrieve/baseline.py`와 `search(mode)`, `generate/evidence.py`, `eval/answer_eval.py`, 질문 세트 형식 및 기획서 예시 질문, 지표 목표 상수, 구조도·기획서를 `docs/`에 추가.
- B용 테스트 기반 추가: `tests/`(샘플 조각·질의, pytest 뼈대), `pytest.ini`, `requirements.txt`에 pytest. `test_retrieval`의 색인 저장 경로 분리는 B가 결정할 사항으로 남김.
- 루트 `README.md` 추가(CLAUDE.md와 이 문서의 내용을 합친 팀 안내). 문서 동기화 대상에 README 포함.
- `README.md`에 단계별 입출력 예시(§2.1)와 단계 표(오프라인/온라인 분리) 추가. 구조 변경 없음.
- `README.md` §2.2 추가: A→B, C→B, B→C 인계 데이터를 한눈에 보는 도식·예시·상호 보장 사항. 구조 변경 없음. (B→C 결과에 한 서비스의 여러 항목이 필요할 때의 조회 방식은 B·C 합의 사항으로 남김)
- C용 테스트 기반 추가: `tests/fixtures/sample_search_results.json`, `fake_search`·`fake_conditions`, `tests/test_generate.py`. `generator.py`에 모듈 속성 호출 규칙 주석 추가. README 8장 '테스트'를 B용/C용으로 나눔.
- 데이터 범위를 청년층으로 한정(§3.1): `config.YOUTH_AGE_RANGE`·`YOUTH_KEYWORDS`, `ingest.store.is_youth_service`(스켈레톤), `pipeline`에 청년 필터 단계 명시. README §1.1 추가, 2.1 예시 수치를 범위에 맞게 조정, §2.2·테스트 절에 상호 링크.
- ③ 임베딩 구현(`index/embedder.py`): `embed_texts(texts, kind="passage")`, `embed_query`, `embedding_dim`, `python -m src.index.embedder` 스모크 테스트. `config`에 `EMBEDDING_DEVICE/BATCH_SIZE/MAX_SEQ_LENGTH` 추가, `requirements.txt`에 sentence-transformers·numpy 추가, `tests/test_embedder.py` 추가.
- ④ 벡터 저장소 구현(`index/vector_store.py`): Chroma + BM25(글자 2-gram), 저장 시 대체값으로 소프트 필터 처리(`build_where`), 증분 색인·삭제·`get_service_chunks`·`clear_index`, `python -m src.index.vector_store --sample --query ...`. `config.INDEX_BATCH_SIZE`, `requirements.txt`에 chromadb·rank_bm25 추가, `tests/test_vector_store.py`와 `fake_embedder`·`tmp_vectorstore`·`indexed_sample` 픽스처 추가, `test_retrieval`이 이를 사용하도록 변경.
- ⑤ 검색 구현(`retrieve/filters.py`, `retrieve/hybrid.py`): RRF 하이브리드, 서비스당 1개, `search_with_info`, 분야→지역→연령 순 완화, `python -m src.retrieve.hybrid`. `config`에 `HYBRID_CANDIDATES`·`RRF_K`·`HYBRID_WEIGHTS` 추가. `test_retrieval`·`test_filters` 확장(RRF, 모드, 완화, 파이썬/저장소 필터 일치).
- 검색 지표 구현(`eval/metrics.py`): `hit_rate_at_k`, `recall_at_k`, `mrr`, `no_evidence_rate`, `evaluate`, `check_targets`. `test_metrics`에 경계 조건(정답 없는 질문, 중복, k 범위, 오류) 추가.
- `python -m eval.metrics` 자체 점검 추가(손계산 예시와 비교, 불일치 시 종료 코드 1).
- 검색 평가 실행 구현(`eval/run_eval.py`): 모드별 실험, K=3/5/10, 범주별·놓친 질문·비교표, 결과 JSON 저장, `--experiment all`, `--today`, `--questions`, `--out-dir`, `--no-save`. `eval/questions.jsonl`에 `filters` 필드 추가(q03). `tests/test_run_eval.py` 추가.
- `tests/fixtures/sample_eval_questions.jsonl` 추가(정답이 샘플 id인 평가 질문). `run_eval`이 정답 없음 오류 시 샘플 실행 방법을 안내. 테스트가 이 파일을 사용.
- `eval/questions.jsonl`을 청년 상황형 질문 초안 34문항(정답은 비움, `topic` 필드 추가)으로 교체. 질문 세트 구성·정답 지정 절차를 README 7장에 기록, `test_run_eval`에 질문 세트 구성 점검 추가.
- 관심 분야를 필터에서 가산점으로 변경(B·C 합의): `vector_store.build_where`와 `filters.apply_filters`에서 field 조건 제거, `filters.relax`는 지역→연령만 풀고 field·situation은 유지(`filters_used`에 그대로 남아 완화 안내에는 지역·연령만 나옴), `filters.field_matches`와 `hybrid.boost_by_field` 추가, `config.FIELD_BOOST`, `schema.Filters` 설명 갱신. 관련 테스트 수정·추가(분야가 달라도 제도가 남는 회귀 테스트 포함). C 코드는 변경 없음.
- `handoff --verify-every N`(A·B 합의, B가 구현): 이벤트 N건을 적용한 뒤 한 번만 검증하고 정확한 토큰 N개를 확인한다. 서비스마다 전체 코퍼스를 읽고 다시 토큰화하는 검증과 매번 전체 대기열(`read_pending`)을 파싱하는 재확인이 이벤트 수 × 코퍼스 크기로 늘어 조각 3만 개 규모에서 병목이었다. 묶음당 한 번: 대기열 재확인, 전후 스냅샷, 무관 서비스 불변 확인, BM25 멤버십·단어 빈도. 이벤트당: 정확한 조각 스냅샷, 모든 조각의 Vector 가시성, 짧은 BM25 질의(서비스명 줄) 한 번. 기본값 1은 기존 동작 그대로. 실패·중단 시 묶음 전체가 대기열에 남아 같은 명령으로 재적용(최대 N건 재작업). 이전 BM25 검증은 조각 전체를 질의로 써서 질의 토큰 수 × 코퍼스 크기로 느렸다. 테스트 추가(`tests/ingest/test_handoff.py`).
- 임베딩 계산 분담(`index/embed_shard.py`, `index/embed_cache.py`): 조각이 약 3만 개로 늘어 한 PC로 오래 걸려, 임베딩만 여러 PC가 나눠 계산한다. `embed_shard run --shard k/N`이 DB를 읽기 전용으로 열어 `service_id` 해시 구간의 조각 텍스트를 계산해 `.npz`(텍스트 해시 → 벡터, 모델·max_seq_length·차원 기록)로 저장(이어하기 가능, 원자적 저장), `merge`/`coverage`로 취합·확인한다. `config.EMBEDDING_CACHE`(환경변수)가 가리키는 파일·폴더를 `embedder.embed_texts`가 먼저 조회하고 없는 텍스트만 모델로 계산하므로 `handoff`·`vector_store`·검증 코드는 그대로다(passage만 대상, 질문은 모델로 계산). 이유: 한 DB는 소비자 하나·색인 하나라는 A의 잠금·ack 구조를 건드리지 않고 병렬화할 수 있는 부분은 임베딩 계산뿐이기 때문. 다른 모델·길이의 파일은 불러올 때 거부, 같은 텍스트의 벡터가 크게 다르면 병합 시 오류. 서비스마다 하는 BM25 전체 검증은 이 방식으로 줄지 않는다(A 영역, 별도 개선 필요).
- 색인 시 지역 보충: A의 지역 어댑터가 없어 실데이터의 `region`이 비어 지역 필터가 작동하지 않았다. `vector_store._to_store_metadata`가 `metadata.region`이 없으면 소관기관명으로 시·도를 추정해(`filters.agency_region`) 저장한다. A가 값을 주면 그것이 우선, 시·도를 알 수 없는 시·군·구와 중앙기관은 비워 두어(통과) 소프트 필터를 유지한다. 이미 색인된 서비스는 다시 색인해야 반영된다(재색인 전에 이 변경을 받을 것). 핸드오프 검증은 같은 함수를 쓰므로 영향 없음.
- 지역 감점: `config.LOCAL_PENALTY`(B·C 합의로 기본 1.0, eval.sweep에서 Hit@5 0.467→0.667·MRR 0.336→0.497), `hybrid.penalize_local`, `filters.is_local`(metadata.region이 있으면 그것, 없으면 소관기관명으로 지자체 추정). 질문에 지역이 없을 때(hybrid_filter)만 지자체 제도 점수에서 값을 빼 전국 제도를 앞세운다. 거르지는 않는다. `eval.sweep --local-penalty`로 효과를 측정하며, C와 합의함(지역을 모르면 전국 위주로 답하고 지역을 되묻는 흐름, 앱은 아직 지역을 받지 않음). `HYBRID_WEIGHTS`도 (1.0, 1.0)→(1.0, 0.2)로 변경(BM25 가중치를 낮출수록 MRR 상승). 이유: A의 지역 어댑터가 아직 region을 채우지 않아(INGEST_A.md) 기존 지역 필터가 실데이터에서 작동하지 않으므로 기관명으로 추정한다.
- `eval/sweep.py` 추가: Hybrid의 BM25 가중치·RRF_K·FIELD_BOOST 조합을 run_eval과 같은 지표로 비교(Vector·BM25 후보는 질문별로 한 번만 계산해 캐시, 합산 단계만 반복). 기본 설정 조합이 run_eval 결과와 같은지 테스트로 확인. `tests/test_sweep.py` 추가.
- `label_helper drop 질문id` 추가: 폐기할 질문을 안전하게 삭제(후속 문항이 있으면 중단, 백업 저장). 테스트 추가.
- `run_eval`이 벡터 실험 전에 "정답 제도가 색인에 없는 질문"을 알려 줌(`missing_gold`). 청년 판정 제외·기한 종료 등으로 정답이 색인에 없으면 검색 개선으로는 못 맞히므로, 낮은 점수를 해석하기 전에 먼저 확인한다.
- `retrieve/baseline.py` 구현: 질문에서 키워드를 뽑아(조사·불용어 제거) A의 `store.sql_keyword_search`(OR LIKE, ID순)로 후보를 가져온 뒤 일치한 키워드 종류 수(서비스명 3배 가중)로 순위를 매겨 서비스당 조각 1개 Top-k 반환. 정형 필터는 쓰지 않음(hybrid_filter와의 차이를 보기 위해). `active_count()` 추가, `run_eval`은 SQLite가 없으면 baseline을 건너뛰고(`baseline` 단독이면 안내 후 종료). `tests/test_baseline.py` 추가.
- 평가 질문 정답을 Claude가 실제 서비스 목록(보조금24 전체 10,969건)을 읽고 1차로 채우고 사람이 검수하는 방식으로 변경(팀 합의). `eval/label_helper.py`에 출처 기록(`set`·`none`의 `--source/--note/--confidence`), 일괄 반영 `apply`, 검수 `review`, `status`의 검수 대기 수를 추가하고 테스트 3개 추가. `eval/questions.jsonl`에 정답과 `label_*`·`reviewed` 필드를 채우고 지역 후속 문항 q35(경기)·q36(서울)을 추가. LABELING.md §8.2.1에 검수 절차 기록.
- 평가 질문 정답 채우기 도구 `eval/label_helper.py`와 `tests/test_label_helper.py`(45개)를 저장소에 추가하고, 사용 안내 `docs/LABELING.md`(준비·정답 기준·대화형 사용법·유형별 가이드·후속 질문·검증·팀 작업 방식)를 작성. README 7장의 정답 채우는 법을 요약+링크로 교체하고 필드(`relevant_service_names`, `follow_up_of`)를 추가.

- 2026-10-21: `policy.py`, bounded `reprocess.py` and compact read-only `coverage.py` expand personal/household scope. Historical replay validates one acquisition run, defers older/unknown observations, preserves exact stored-event handoff and never closes missing IDs. B codec/region fallback/ranking and C contracts remain unchanged.

- 2026-10-22: Canonical region/provenance, selected metadata reconciliation, Korean full-date grammar and transaction-bound segmented raw replay/resume. Preserve batch verification, expanded scope, ranking and evidence contracts; inspect local index read-only without claiming a historical build revision.

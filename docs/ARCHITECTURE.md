# 코드 구조와 설계 이유

> 코드 구조가 바뀌면 이 문서도 같은 커밋에서 갱신한다 (`CLAUDE.md` '문서 동기화').
> 설계 근거는 [`사전기획서.docx`](사전기획서.docx)(이하 기획서), 프로젝트 규칙은 `CLAUDE.md`.

## 1. 전체 구조 — 색인과 질의응답

![RAG 전체 구조](images/rag-structure.png)

색인은 문서(데이터)가 바뀔 때만 다시 돌리고, 질문은 아래 온라인 경로만 돈다. 구조도의 단계(①~⑥)와 코드의 대응:

| 단계 | 구조도 | 코드 | 담당 |
|---|---|---|---|
| 오프라인 | 문서(PDF) → ① 로드 | `src/ingest/client.py`, `store.py` (우리 데이터는 PDF가 아니라 보조금24 API → DB) | A |
| | ② 분할 | `src/ingest/chunker.py` | A |
| | ③ 임베딩 | `src/index/embedder.py` | B |
| | ④ 벡터 저장소 | `src/index/vector_store.py` | B |
| 온라인 | (질문 → 조건 추출) | `src/generate/condition_extractor.py` — 구조도에는 없고 기획서 8.1에서 추가 | C |
| | ⑤ 검색 Top-K | `src/retrieve/hybrid.py`, `filters.py` | B |
| | (근거 선별) | `src/generate/evidence.py` | C |
| | ⑥ 생성 LLM → 답변 + 출처 | `src/generate/generator.py`, `citation.py` | C |

구조도 대비 달라진 점: ① 입력이 PDF가 아닌 API이고 정형 지원조건이 함께 들어온다 / 검색 앞에 조건 추출과 정형 필터가 붙는다 / 검색과 생성 사이에 근거 선별이 있다.

## 2. 디렉터리 구조

```
src/
  common/    config.py, schema.py                       # 전원 공용: 설정, 모듈 간 데이터 계약
  ingest/    client, store, chunker, pipeline           # A: ① 로드, ② 분할, 정기 수집
  index/     embedder, vector_store                     # B: ③ 임베딩, ④ 벡터 저장소
  retrieve/  hybrid, filters, baseline                  # B: ⑤ 검색, 정형 필터, 비교 실험용 Baseline
  generate/  condition_extractor, evidence, generator, citation   # C
app/         main.py                                    # C: UI
eval/        metrics, answer_eval, run_eval, questions.jsonl, results/
tests/       pytest. fixtures/(가상 샘플 조각·질의), conftest.py, test_*.py
data/raw/    원본 응답 (커밋 금지)
docs/        사전기획서.docx, ARCHITECTURE.md, images/
```

## 3. 왜 이렇게 나눴나

| 결정 | 이유 (기획서 근거) |
|---|---|
| 디렉터리 = 담당자 단위 (A `ingest` / B `index`+`retrieve`+`eval` / C `generate`+`app`) | `CLAUDE.md` 담당 영역과 1:1. 한 사람이 자기 폴더만 수정해 파일 충돌과 책임 논쟁을 없앤다. |
| `src/common/schema.py`에 `Chunk`·`Metadata`·`Filters`·`Recommendation`·`Answer`를 한 곳에 정의 | 모듈 간 계약(A→B 조각, C→B 필터, B→C 검색 결과, C→UI 답변)을 코드로 고정해 구현 전에도 병렬 개발한다. 바꿀 때는 이 파일과 `CLAUDE.md` 인터페이스 절부터 고친다. |
| 함수는 시그니처·docstring만 두고 `NotImplementedError` | 인터페이스만 합의하고 구현은 담당자가 한다. 합의 전인 부분(DB 스키마, 색인 진입점)은 확정하지 않는다. |
| `ingest`를 `client` / `store` / `chunker` / `pipeline`으로 분리 | ①로드와 ②분할에 대응. 수집은 API 의존, 분할은 순수 함수라 따로 테스트한다. DB는 정형 필터·Baseline 실험·변경 감지의 원천이다 (7장). |
| `store.upsert_services`가 변경된 `service_id`를 반환, `pipeline.run`은 기본 증분 | 기획서 7장: 1일 1회 정기 수집 후 **변경분만 재색인**. 종료된 서비스는 `mark_closed` → `vector_store.remove_services`로 색인에서 뺀다. |
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
| `test_filters`, `test_metrics`, `test_retrieval`, `test_fixtures` | B 코드(필터 규칙, 지표 계산, 색인·검색 종단)와 샘플 데이터가 데이터 계약을 지키는지 확인. 필터·지표 테스트는 참조 구현으로 통과를 확인했다. |
| `data/raw/`, `.env`는 `.gitignore` | 원본 응답과 API 키는 커밋 금지. `.env.example`로 필요한 키만 공유. |

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
| `ingest/client.py` | 구현됨 (API 호출·페이지 순회·원본 저장). 실제 응답 필드는 검증 전 |
| `common/config.py`, `common/schema.py`, `eval/run_eval.load_questions` | 구현됨 |
| `tests/` | 샘플 데이터 검증 3개 통과, 나머지 16개는 구현 전이라 skip |
| 그 외 | 스켈레톤 (`NotImplementedError`) |

## 7. 변경 이력

- 초기 스켈레톤 작성. API 연결 테스트용 `youth_benefits.py` 삭제.
- 기획서·RAG 구조도 반영: `Metadata`/`Filters`/`Recommendation` 추가, `Answer` 구조 확장, 증분 수집·종료 처리(`store`, `pipeline`), Recursive 분할, `retrieve/baseline.py`와 `search(mode)`, `generate/evidence.py`, `eval/answer_eval.py`, 질문 세트 형식 및 기획서 예시 질문, 지표 목표 상수, 구조도·기획서를 `docs/`에 추가.
- B용 테스트 기반 추가: `tests/`(샘플 조각·질의, pytest 뼈대), `pytest.ini`, `requirements.txt`에 pytest. `test_retrieval`의 색인 저장 경로 분리는 B가 결정할 사항으로 남김.
- 루트 `README.md` 추가(CLAUDE.md와 이 문서의 내용을 합친 팀 안내). 문서 동기화 대상에 README 포함.
- `README.md`에 단계별 입출력 예시(§2.1)와 단계 표(오프라인/온라인 분리) 추가. 구조 변경 없음.
- `README.md` §2.2 추가: A→B, C→B, B→C 인계 데이터를 한눈에 보는 도식·예시·상호 보장 사항. 구조 변경 없음. (B→C 결과에 한 서비스의 여러 항목이 필요할 때의 조회 방식은 B·C 합의 사항으로 남김)

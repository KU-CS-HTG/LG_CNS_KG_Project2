# 코드 구조와 설계 이유

> 이 문서는 코드 구조가 바뀔 때 함께 갱신한다(규칙: `CLAUDE.md` '문서 동기화').
> 원 설계 근거는 `docs/사전기획서.docx`, 프로젝트 규칙은 `CLAUDE.md`.

## 1. 디렉터리 구조

```
src/
  common/    config.py, schema.py        # 전원 공용: 설정, 모듈 간 데이터 계약
  ingest/    client, store, chunker, pipeline   # A: ① 로드, ② 분할
  index/     embedder, vector_store             # B: ③ 임베딩, ④ 벡터 저장소
  retrieve/  filters, hybrid                    # B: ⑤ 검색, 정형 필터
  generate/  condition_extractor, generator, citation   # C: 조건 추출, ⑥ 생성, 인용
app/         main.py                     # C: UI
eval/        metrics, run_eval, questions.jsonl   # B(검색)·C(답변)·전원(질문 세트)
data/raw/    원본 응답 (커밋 금지)
```

## 2. 왜 이렇게 나눴나

| 결정 | 이유 |
|---|---|
| 디렉터리 = 담당자 단위 (A: `ingest`, B: `index`+`retrieve`+`eval`, C: `generate`+`app`) | `CLAUDE.md`의 담당 영역과 1:1로 맞춰 한 사람이 자기 폴더만 수정하게 한다. 파일 충돌과 "누가 이 파일 주인인가" 논의를 없앤다. |
| `src/common/schema.py`에 `Chunk`/`Citation`/`Answer`를 한 곳에 정의 | 모듈 간 계약(A→B 조각 레코드, B→C `search()` 결과, C의 최종 출력)을 코드로 고정한다. 구현 전에도 서로의 입출력을 가정하고 병렬 개발할 수 있다. 바꿀 때는 이 파일 + `CLAUDE.md` 인터페이스 절부터 고친다. |
| 함수는 시그니처·docstring만 두고 `NotImplementedError` | 인터페이스는 합의하되 구현은 각 담당자가 한다. 아직 합의 전인 부분(DB 스키마, 색인 진입점)을 임의로 확정하지 않는다. |
| `ingest`를 `client`(수집) / `store`(정제·DB) / `chunker`(분할) / `pipeline`(진입점)으로 분리 | 사전기획서 ①로드와 ②분할 단계에 대응. 수집은 API 의존, 분할은 순수 함수라 따로 테스트할 수 있다. DB는 정형 필터와 Baseline(SQL+키워드) 평가의 원천이기도 하다. |
| `index`(오프라인 색인)와 `retrieve`(온라인 검색) 분리 | 색인은 문서가 바뀔 때만, 검색은 질문마다 실행되는 구조(RAG 구조도의 오프라인/온라인)를 코드 경계에 그대로 반영. |
| `retrieve/filters.py`를 `hybrid.py`와 별도 파일로 | 정형 필터는 소프트 적용·완화(relax)·기한 지난 제도 제외 등 규칙이 독립적이고, 평가에서 "Hybrid" vs "Hybrid+정형 필터"를 따로 비교해야 한다. |
| `generate/`를 `condition_extractor` / `generator` / `citation`으로 분리 | 조건 추출(LLM), 근거 기반 생성, 출처 형식은 각각 평가·수정 주기가 다르다. 인용 형식은 Citation Accuracy 평가 대상이라 독립시켰다. |
| `app/`은 `answer_question()` 하나만 호출 | UI가 내부 파이프라인에 의존하지 않게 해서 UI 교체와 백엔드 수정이 서로 영향을 주지 않는다. |
| `eval/`을 별도 최상위로 | 검색 지표(B)와 답변 지표(C), 질문 세트(전원)가 섞이는 공용 영역이라 어느 한 담당 폴더에 두지 않는다. |
| `data/raw/`, `.env`는 `.gitignore` | 규칙: 원본 응답과 API 키는 커밋 금지. `.env.example`로 필요한 키 목록만 공유. |

## 3. 데이터 흐름

오프라인(문서 변경 시): `ingest.client` → `ingest.store`(DB) → `ingest.chunker`(`Chunk`) → `index.embedder` → `index.vector_store`

온라인(질문마다): 질문 → `generate.condition_extractor`(조건) → `retrieve.hybrid.search`(필터+Vector+BM25) → `generate.generator`(답변) + `generate.citation` → `Answer`

## 4. 구현 현황

| 모듈 | 상태 |
|---|---|
| `ingest/client.py` | 구현됨 (API 호출·페이지 순회·원본 저장). 단, 실제 API 응답 필드는 아직 검증 전 |
| 그 외 전부 | 스켈레톤 (`NotImplementedError`) |

## 5. 변경 이력

- 초기 스켈레톤 작성. API 연결 테스트용 `youth_benefits.py` 삭제.

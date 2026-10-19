# 프로젝트: 개인 맞춤형 정부 지원제도 탐색 및 안내 서비스 (RAG)

사용자가 생활 상황을 자연어로 질문하면, 공공데이터에서 해당 가능성이 있는 정부 지원제도를 찾아 자격조건·신청 절차를 **출처와 함께** 안내하는 RAG 서비스. **1차 데이터 범위는 청년층 대상 제도**(README 1.1). 설계 근거는 `docs/사전기획서.docx`(1~9장)를 따른다.

## 데이터
- 출처: 공공데이터포털 "행정안전부_대한민국 공공서비스(혜택) 정보"(보조금24) REST API (전체 약 1만 건 중 **청년 대상만 사용**. 판정은 `ingest.store.is_youth_service`, 기준값은 `config.YOUTH_*` — 제안 단계, 팀 합의 후 확정)
- 서비스 1건 = 서비스명, 지원대상, 선정기준, 지원내용, 신청방법, 신청기한, 소관기관, 상세 링크 + 지원조건(정형)
- 원본 응답은 `data/raw/`에 저장하고 커밋하지 않는다. API 키는 `.env`에만 둔다(커밋 금지).

## 파이프라인 (RAG 구조도 기준)
오프라인 색인 — 문서가 바뀔 때만 다시 실행
1. ① 로드: API 수집·정제 → 청년 대상 필터 → DB 적재
2. ② 분할: 서비스 항목별(지원대상/선정기준/지원내용/신청방법) 구조 기반 분할, 조각 앞에 서비스명 접두
3. ③ 임베딩: 로컬 다국어 모델(bge-m3 우선, 자원 부족 시 multilingual-e5)
4. ④ 벡터 저장소: 조각 + 메타데이터(지역·소관기관·분야·신청기한·대상조건) 저장

온라인 질의응답 — 질문마다 실행
5. 질문 → 조건 추출(LLM, 나이·거주지·상황·관심 분야) → 정형 조건 필터링
6. ⑤ 검색: Hybrid(Vector + BM25), Top-5, 중복 제거
7. 근거 선별 → ⑥ 생성(gpt-4o-mini) → 답변 + 출처

## 담당 영역 (담당자 이름은 팀에서 채울 것)
| 담당 | 단계 | 디렉터리 |
|---|---|---|
| A (데이터·색인) 담당자: | ① 로드, ② 분할, 메타데이터 설계, 정기 수집 | `src/ingest/` |
| B (임베딩·검색) 담당자: | ③ 임베딩, ④ 벡터 저장소, ⑤ 검색, 정형 필터, Baseline, 검색 평가 | `src/index/`, `src/retrieve/`, `eval/metrics.py`·`eval/run_eval.py` |
| C (생성·서비스) 담당자: | 조건 추출, 근거 선별, ⑥ 생성, 인용 형식, UI, 답변 평가 | `src/generate/`, `app/`, `eval/answer_eval.py` |
- 테스트(`tests/`)는 각자 자기 모듈의 `test_*.py`를 관리하고, 샘플 데이터(`tests/fixtures/`)는 A의 실제 조각이 나오면 A·B가, B의 실제 검색 결과가 나오면 B·C가 함께 실제 데이터로 보강한다. C는 다른 모듈 함수를 모듈 속성(`hybrid.search(...)`)으로 호출한다(테스트에서 가짜로 바꿔 끼우기 위함).
- 평가 질문 세트(`eval/questions.jsonl`)와 8.3 기술 선택 기록은 전원이 함께 작성한다.

## 모듈 간 인터페이스 (제안 — 팀 합의 후 확정, 바꿀 때는 이 파일부터 고친다)
- A → B: 조각 단위 레코드 `{chunk_id, service_id, service_name, section, text, metadata, detail_url}`
- C → B: `Filters`(나이·지역·분야·상황·기준일, 값이 없으면 키 생략)
- B → C: `search(query: str, filters: Filters, k: int = 5, mode: str = "hybrid_filter") -> list[Chunk]` (`mode`는 평가 실험용, 서비스는 기본값 사용)
- C의 최종 출력: `Answer` = 답변 텍스트 + 제도별 `Recommendation`(해당 가능 이유, 지원내용, 신청방법·기한, 확인 필요 조건, 근거 목록[서비스명·소관기관·근거 문구·상세 링크]) + `no_evidence`
- 위 타입은 모두 `src/common/schema.py`에 있다. 코드 구조와 설계 이유는 `docs/ARCHITECTURE.md`, 구조도는 `docs/images/rag-structure.png`.

## 답변 생성 규칙 (사전기획서 6장·8장 기준)
- 검색된 근거 안에서만 답한다. 근거가 없으면 모른다고 답하고 담당 기관·공식 사이트 확인을 안내한다.
- 자격이 불명확하거나 정형 조건 값이 비어 있으면 단정하지 않고 "확인 필요"로 표시한다.
- 모든 답변에 서비스명·소관기관·근거 문구·상세 링크를 붙이고, "최종 신청 자격은 담당 기관에서 확인" 안내를 넣는다.
- 신청기한이 지난 제도는 후보에서 제외한다. 정형 필터는 하드가 아니라 소프트로 적용하고, 결과가 없으면 필터를 풀어 다시 검색한다.
- 사용자에게 보이는 문구는 한국어로 작성한다.

## 평가 (사전기획서 9장)
- 검색: Hit Rate@K, Recall@K, MRR / 답변: Answer Correctness, Faithfulness, Citation Accuracy
- 실험은 Baseline(SQL+키워드) → Vector → Hybrid+정형 필터 → 최종 순서로 9.4에 기록한다.

## 작업 규칙
- 자기 담당 디렉터리 밖의 파일을 고칠 때는 담당자와 먼저 합의한다.
- 변경 전에 `docs/사전기획서.docx`의 해당 절을 확인하고, 설계가 바뀌면 기획서(8.3)에도 기록한다.
- 새 라이브러리를 추가하면 `requirements.txt`를 함께 갱신한다.
- **문서 동기화(자동)**: 디렉터리·모듈·함수 시그니처·데이터 계약(`src/common/schema.py`)·담당 영역·명령을 바꾸는 작업을 하면, 같은 커밋에서 `README.md`(팀 안내), `docs/ARCHITECTURE.md`(구조와 이유·변경 이력)와 이 파일의 해당 절을 함께 갱신한다. 문서만 빠진 커밋을 만들지 않는다.

## 자주 쓰는 명령
```
pip install -r requirements.txt        # 설치 (.env.example을 .env로 복사 후 키 입력)
python -m src.ingest.pipeline          # A: 수집·정제·분할 (기본 증분, --full은 전체)
python -m src.index.embedder           # B: 임베딩 모델 동작 확인 (첫 실행 시 모델 다운로드)
python -m src.index.vector_store       # B: 색인 (진입점은 B가 확정)
python -m app.main                     # C: 앱 실행
python -m pytest -rs                   # 테스트 (미구현은 skip. tests/fixtures 샘플로 A 없이 B, B 없이 C 검증 가능)
python -m eval.run_eval --experiment hybrid_filter --k 5   # 평가 (baseline|vector|bm25|hybrid|hybrid_filter)
```
(스켈레톤 단계라 대부분 `NotImplementedError`. 구현하면서 갱신할 것)

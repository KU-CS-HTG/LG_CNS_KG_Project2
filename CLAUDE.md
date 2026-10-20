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
- 평가 질문 세트(`eval/questions.jsonl`)와 8.3 기술 선택 기록은 전원이 함께 작성한다. 정답(`relevant_service_ids`)은 **데이터를 읽고 사람이 정하며**(검색 결과를 베끼지 않는다), `eval/label_helper.py`로 채우고 기준은 `docs/LABELING.md`를 따른다(질문당 5개 이내, 지역 없는 질문은 전국 단위 제도, 지역 제도는 `followup` 문항).

## 모듈 간 인터페이스 (제안 — 팀 합의 후 확정, 바꿀 때는 이 파일부터 고친다)
- A → B: 조각 단위 레코드 `{chunk_id, service_id, service_name, section, text, metadata, detail_url}`
- C → B: `Filters`(나이·지역·분야·상황·기준일, 값이 없으면 키 생략). **거르는 것은 나이·지역·기준일**, 분야(`field`)는 필터가 아니라 맞는 제도에 가산점(`config.FIELD_BOOST`), 상황은 현재 검색에 쓰이지 않음
- B → C: `search(query: str, filters: Filters, k: int = 5, mode: str = "hybrid_filter") -> list[Chunk]` (`mode`는 평가 실험용, 서비스는 기본값 사용). 서비스당 조각 1개·관련도 순, 결과가 없으면 필터를 풀어 재검색(지역→연령, 분야·상황은 필터가 아니라 풀지 않음), 그래도 없으면 `[]`. 완화 여부가 필요하면 `hybrid.search_with_info(...)`(점수·`filters_used`·`relaxed` 포함)
- B → C (implemented and used by `evidence.select_evidence`): `vector_store.get_service_chunks(service_id) -> list[Chunk]` — 검색 결과에 없던 같은 서비스의 다른 항목 조각이 필요할 때. 필터는 `vector_store.build_where(filters)`로 저장소 조건이 된다.
- C의 최종 출력: `Answer` = 답변 텍스트 + 제도별 `Recommendation`(해당 가능 이유, 지원내용, 신청방법·기한, 확인 필요 조건, 근거 목록[서비스명·소관기관·근거 문구·상세 링크]) + `no_evidence`
- 위 타입은 모두 `src/common/schema.py`에 있다. 코드 구조와 설계 이유는 `docs/ARCHITECTURE.md`, 구조도는 `docs/images/rag-structure.png`.
- A 구현 계약: `docs/INGEST_A.md`. `pipeline.run(full=False)`는 기존 `list[Chunk]`를 유지하고 이전 미확인 조각도 재전달한다. B는 `store.get_pending_changes()`의 서비스 전체 교체/제거를 두 색인에 적용한 후 service_id+version+event_id로 `acknowledge_changes()`한다. The A-owned `handoff` consumer applies stored event snapshots through existing B APIs, verifies both retrieval paths and acknowledges exact tokens. Real Chroma/fake-embedding integration is tested; actual-model and production-data integration remain unverified.
- `mark_closed()` 단독 호출은 안전하게 아무 작업도 하지 않는다. 내부 `apply_run()`은 검증된 완전 목록으로만 소실을 판정하며, 현재 API 세부 계약 미검증 상태에서는 자동 소실이 차단된다. `--full`은 활성 조각 재생성이고 DB·이력·pending 제거를 보존한다.
- A 2차 수정: `merge.py`는 현재 관측과 과거 fallback을 구분하며 상세 실패 때 현재 목록의 공통 값이 과거 상세보다 우선한다. 명시적 빈 값·정상 행의 키 누락·응답 실패를 구분하고 필드 출처/충돌을 A 저장소에 남긴다. 두 현재 원천의 우선순위와 기한 충돌 정책은 `docs/INGEST_A.md` 참조. 수정일시를 필드별 최신성 근거로 추정하지 않는다.
- `storage.StoragePaths`를 전달해 config 전역 변경 없이 실행별 DB/raw를 선택한다. fixture/validation의 운영 DB·raw·sidecar·lock 경로 및 resolved 별칭은 쓰기 전에 거부한다. `run_fixture()`와 CLI에 같은 격리를 적용한다. HTMLParser는 표 셀/행 경계를 보존하며 정제 변경은 다음 관측 시 한 번의 해시/전체 조각 갱신을 유발할 수 있다.
- 격리 live 표본은 세 엔드포인트 3건씩 확인했으나 전체 종료·빈 스냅샷·실제 연령/지역 어댑터는 미검증이다. JA0110/JA0111 기본 라벨은 OAS에서 확인했어도 신청자/부양자 의미 등을 자동 추정하지 않는다.
- Supported handoff uses one worker per target index, holding index→source-DB OS locks through apply/verify/ack. `pipeline._execute`, `store.upsert_services` and public `store.apply_run` share the source lock; the pipeline calls `_apply_run_locked` only while holding it. Competing processes fail immediately. Stale-ack protection alone does not synchronize arbitrary direct B writers. provenance 품질은 `store.get_record(service_id)`로 조회하고 후보 보류·C 표시 방침은 합의한다.

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
python -m src.ingest.pipeline          # A: 실제 API 수집 (실행 전 승인 필요), 기본 증분
python -m src.ingest.pipeline --full   # A: 활성 조각 재생성, DB 초기화 없음
python -m src.ingest.pipeline --offline-fixture tests/fixtures/a_synthetic_api.json  # 격리된 합성 실행
python -m src.ingest.pipeline --validation --sample-pages 1 --per-page 3  # 승인 범위 내 격리 live 표본
python -m pytest tests/ingest -q -ra   # A: mock HTTP·임시 SQLite, 미구현 skip 금지
python -m src.ingest.handoff --dry-run # inspect pending; no index/model initialization
python -m src.ingest.handoff --apply   # cached model only, apply→verify→exact ack; stops on first failure
python -m src.index.vector_store       # legacy direct B CLI; bypasses consumer locking/ack
python -m app.main                     # C: 앱 실행
python -m pytest -rs                   # 테스트 (미구현은 skip. tests/fixtures 샘플로 A 없이 B, B 없이 C 검증 가능)
python -m eval.label_helper label --only q01,q02   # 평가 질문 정답 채우기 (사용법: docs/LABELING.md. fetch→label→check)
python -m eval.label_helper review                 # Claude 1차 정답의 사람 검수 대기 목록 (--ok q01,q02 로 완료 표시)
python -m eval.run_eval --experiment hybrid_filter --k 5   # 검색 평가 (baseline|vector|bm25|hybrid|hybrid_filter|all, all은 비교표 출력). 정답이 채워진 질문 세트와 색인 필요
```
Handoff settings: `--db-path`, `--index-dir`, `--model`; `--validation` requires explicit isolated paths and rejects live destinations/aliases before writes. Fixture/validation source runs require validation mode. One source DB ↔ one target/model is recorded in `<db>.handoff.json` and `<index>/handoff_manifest.json`; changing an association is not an automatic migration. Existing nonempty unbound indexes require `--adopt-existing`; verify the selected model is the original model, since an old collection slug alone cannot prove its identity. Model dimension/max sequence length/cosine settings must agree. Worker-only config changes and offline Hugging Face flags prevent caller cache/config leakage and model downloads. Raw data, DB/Chroma files, manifests and locks stay out of Git.

Current verification boundary: A/B/C are implemented; stored-event integration is tested using real Chroma and fake embeddings. Full API compatibility, production age/region adapters, actual-model verification and search/answer quality evaluation remain separate work. The previous live smoke record is historical, not a live run in this handoff task.

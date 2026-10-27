# 프로젝트: 개인 맞춤형 정부 지원제도 탐색 및 안내 서비스 (RAG)

사용자가 생활 상황을 자연어로 질문하면, 공공데이터에서 해당 가능성이 있는 정부 지원제도를 찾아 자격조건·신청 절차를 **출처와 함께** 안내하는 RAG 서비스. **Dataset scope includes general personal/household support for young users**(README 1.1). 설계 근거는 `docs/사전기획서.docx`(1~9장)를 따른다.

## 데이터
- 출처: 공공데이터포털 "행정안전부_대한민국 공공서비스(혜택) 정보"(보조금24) REST API (general personal/household support included; `ingest.policy.inclusion_decision` / `personal-household-v1`. Youth matching remains a tag, not the dataset gate)
- 서비스 1건 = 서비스명, 지원대상, 선정기준, 지원내용, 신청방법, 신청기한, 소관기관, 상세 링크 + 지원조건(정형)
- 원본 응답은 `data/raw/`에 저장하고 커밋하지 않는다. API 키는 `.env`에만 둔다(커밋 금지).
- 판정 기준 문서(제안, 미구현): 공무원 판정용 고시·업무처리기준·사업안내 지침 PDF. 공식 기관 원본만 쓰고(블로그·요약 금지), 원본은 `data/guides/`에 두고 커밋하지 않는다. 문서 목록(`doc_id`·`file`·문서명·발행 기관·연도·출처·공공누리 유형·적용 범위(전국/지역)·연결 `service_id`·`sha256` 파일 지문)은 `src/ingest/guide_sources.json`(커밋)에 둔다. 연결 `service_id`는 사람이 문서를 읽고 정한다. PDF는 공유 드라이브 zip으로 나누고 각자 `data/guides/`에 풀며, 파싱 전에 `sha256`으로 같은 파일인지 확인한다(다르면 중단).

## 파이프라인 (RAG 구조도 기준)
오프라인 색인 — 문서가 바뀔 때만 다시 실행
1. ① 로드: API collection/normalization → versioned personal/household scope → SQLite
2. ② 분할: 서비스 항목별(지원대상/선정기준/지원내용/신청방법) 구조 기반 분할, 조각 앞에 서비스명 접두
3. ③ 임베딩: 로컬 다국어 모델(bge-m3 우선, 자원 부족 시 multilingual-e5)
4. ④ 벡터 저장소: 조각 + 메타데이터(지역·소관기관·분야·신청기한·대상조건) 저장

온라인 질의응답 — 질문마다 실행
5. 질문 → 조건 추출(LLM, 나이·거주지·상황·관심 분야) → 정형 조건 필터링
6. ⑤ 검색: Hybrid(Vector + BM25), Top-5, 중복 제거
7. 근거 선별 → ⑥ 생성(gpt-4o-mini) → 답변 + 출처

판정 기준 문서 경로(제안) — 기존 보조금24 경로와 분리, 기존 `Chunk`·`search`·handoff는 바꾸지 않는다
- 오프라인: PDF → 조항·소제목 단위 분할(쪽 번호 유지, "문서명 > 소제목" 접두) → 문서 전용 색인
- 온라인: 추천 카드의 '확인 필요' 항목·후속 질문 → `guide_store.search_guides` → 판정 근거(문서명·쪽)

## 담당 영역 (담당자 이름은 팀에서 채울 것)
| 담당 | 단계 | 디렉터리 |
|---|---|---|
| A (데이터·색인) 담당자: | ① 로드, ② 분할, 메타데이터 설계, 정기 수집 | `src/ingest/` |
| B (임베딩·검색) 담당자: | ③ 임베딩, ④ 벡터 저장소, ⑤ 검색, 정형 필터, Baseline, 검색 평가 | `src/index/`, `src/retrieve/`, `eval/metrics.py`·`eval/run_eval.py` |
| C (생성·서비스) 담당자: | 조건 추출, 근거 선별, ⑥ 생성, 인용 형식, UI, 답변 평가 | `src/generate/`, `app/`, `eval/answer_eval.py` |
- 테스트(`tests/`)는 각자 자기 모듈의 `test_*.py`를 관리하고, 샘플 데이터(`tests/fixtures/`)는 A의 실제 조각이 나오면 A·B가, B의 실제 검색 결과가 나오면 B·C가 함께 실제 데이터로 보강한다. C는 다른 모듈 함수를 모듈 속성(`hybrid.search(...)`)으로 호출한다(테스트에서 가짜로 바꿔 끼우기 위함).
- 평가 질문 세트(`eval/questions.jsonl`)와 8.3 기술 선택 기록은 전원이 함께 작성한다. 정답(`relevant_service_ids`)은 **데이터를 읽고 사람이 정하며**(검색 결과를 베끼지 않는다), `eval/label_helper.py`로 채우고 기준은 `docs/LABELING.md`를 따른다(질문당 5개 이내, 지역 없는 질문은 전국 단위 제도, 지역 제도는 `followup` 문항).
- 판정 기준 문서(제안): A `src/ingest/guides.py`·`guide_sources.json` / B `src/index/guide_store.py` / C `src/generate/guide_evidence.py`. 각자 새 파일로 만들고 A·B 기존 파일은 고치지 않는다. 평가 질문은 `eval/guide_questions.jsonl`(기존 `questions.jsonl`과 분리).

## 모듈 간 인터페이스 (제안 — 팀 합의 후 확정, 바꿀 때는 이 파일부터 고친다)
- A → B: 조각 단위 레코드 `{chunk_id, service_id, service_name, section, text, metadata, detail_url}`
- C → B: `Filters`(나이·지역·분야·상황·기준일, 값이 없으면 키 생략). **거르는 것은 나이·지역·기준일**, 분야(`field`)는 필터가 아니라 맞는 제도에 가산점(`config.FIELD_BOOST`), 상황은 현재 검색에 쓰이지 않음
- B → C: `search(query: str, filters: Filters, k: int = 5, mode: str = "hybrid_filter") -> list[Chunk]` (`mode`는 평가 실험용, 서비스는 기본값 사용). 서비스당 조각 1개·관련도 순, **질문에 지역이 없으면 지자체 제도를 뒤로 보내 전국 제도를 먼저 준다**(`config.LOCAL_PENALTY`, 제외는 아님. 지역을 되묻는 것은 C의 응답 흐름), 결과가 없으면 필터를 풀어 재검색(지역→연령, 분야·상황은 필터가 아니라 풀지 않음), 그래도 없으면 `[]`. 완화 여부가 필요하면 `hybrid.search_with_info(...)`(점수·`filters_used`·`relaxed` 포함)
- B → C (implemented and used by `evidence.select_evidence`): `vector_store.get_service_chunks(service_id) -> list[Chunk]` — 검색 결과에 없던 같은 서비스의 다른 항목 조각이 필요할 때. 필터는 `vector_store.build_where(filters)`로 저장소 조건이 된다. Stored region is canonical A evidence first, then agency inference; optional region_provenance survives retrieval and C readers. Neither agency nor legacy values prove eligibility.
- C의 최종 출력: `Answer` = 답변 텍스트 + 제도별 `Recommendation`(해당 가능 이유, 지원내용, 신청방법·기한, 확인 필요 조건, 근거 목록[서비스명·소관기관·근거 문구·상세 링크]) + `no_evidence`
- 위 타입은 모두 `src/common/schema.py`에 있다. 코드 구조와 설계 이유는 `docs/ARCHITECTURE.md`, 구조도는 `docs/images/rag-structure.png`.
- A 구현 계약: `docs/INGEST_A.md`. `pipeline.run(full=False)`는 기존 `list[Chunk]`를 유지하고 이전 미확인 조각도 재전달한다. B는 `store.get_pending_changes()`의 서비스 전체 교체/제거를 두 색인에 적용한 후 service_id+version+event_id로 `acknowledge_changes()`한다. The A-owned `handoff` consumer applies stored event snapshots through existing B APIs, verifies both retrieval paths and acknowledges exact tokens. Real Chroma/fake-embedding integration is tested; actual-model and production-data integration remain unverified.
- `mark_closed()` 단독 호출은 안전하게 아무 작업도 하지 않는다. 내부 `apply_run()`은 검증된 완전 목록으로만 소실을 판정하며, 현재 API 세부 계약 미검증 상태에서는 자동 소실이 차단된다. `--full` still collects APIs and requeues active chunks without resetting DB/history/pending. It does not restore discarded excluded content; use historical raw reprocessing.
- A 2차 수정: `merge.py`는 현재 관측과 과거 fallback을 구분하며 상세 실패 때 현재 목록의 공통 값이 과거 상세보다 우선한다. 명시적 빈 값·정상 행의 키 누락·응답 실패를 구분하고 필드 출처/충돌을 A 저장소에 남긴다. 두 현재 원천의 우선순위와 기한 충돌 정책은 `docs/INGEST_A.md` 참조. 수정일시를 필드별 최신성 근거로 추정하지 않는다.
- `storage.StoragePaths`를 전달해 config 전역 변경 없이 실행별 DB/raw를 선택한다. fixture/validation의 운영 DB·raw·sidecar·lock 경로 및 resolved 별칭은 쓰기 전에 거부한다. `run_fixture()`와 CLI에 같은 격리를 적용한다. HTMLParser는 표 셀/행 경계를 보존하며 정제 변경은 다음 관측 시 한 번의 해시/전체 조각 갱신을 유발할 수 있다.
- 격리 live 표본은 세 엔드포인트 3건씩 확인했으나 전체 종료·빈 스냅샷·실제 연령/지역 어댑터는 미검증이다. JA0110/JA0111 기본 라벨은 OAS에서 확인했어도 신청자/부양자 의미 등을 자동 추정하지 않는다.
- Supported handoff uses one worker per target index, holding index→source-DB OS locks through apply/verify/ack. `pipeline._execute`, `store.upsert_services` and public `store.apply_run` share the source lock; the pipeline calls `_apply_run_locked` only while holding it. Competing processes fail immediately. Stale-ack protection alone does not synchronize arbitrary direct B writers. provenance 품질은 `store.get_record(service_id)`로 조회하고 후보 보류·C 표시 방침은 합의한다.
- 판정 기준 문서(계약 확정 2026-10-23, 타입은 `src/common/schema.py`. 구현은 진행 중):
  - A → B: `GuideChunk{chunk_id, doc_id, doc_title, publisher, year, page, heading, text, scope, service_ids, source_url}`. `scope`는 "전국" 또는 지역명, `service_ids`가 비면 공통 기준 문서.
  - B → C: `guide_store.search_guides(query, service_ids=None, region=None, k=3) -> list[GuideChunk]`. Vector+BM25(RRF), `service_ids`가 있으면 그 제도의 문서와 공통 기준 문서만, 전국 문서는 항상 통과·지역 문서는 지역이 맞을 때만, 없으면 `[]`.
  - C 출력: `Citation`에 선택 필드 `kind`("service" 기본, "guide")·`page`(guide일 때 쪽 번호), `Recommendation`에 선택 필드 `service_id`(기본 "", 카드에서 `search_guides`의 `service_ids`로 쓴다). 기존 필드·기본값은 그대로다.

## Dataset inclusion and historical replay (2026-10-21)

`policy.py` owns include/provisional/exclude decisions independently of labels and applicant eligibility.
General household/dependent benefits remain included; only explicit institution-only target
assertions support exclusion. Ambiguity is provisional inclusion, not ineligibility.
`reprocess.py` reuses ONE A raw acquisition run, validates summaries/pages/IDs, uses the
existing merge/normalize/store/chunker path and creates complete pending snapshots.
Historical observations never become fresh or authorize disappearance. Older/unknown-order
observations defer; an API-disappeared record needs a new observation. Run source/index
inspection with `python -m src.ingest.coverage`; exact commands and limits: `docs/INGEST_A.md`.
No abandoned readiness/packaging/telemetry subsystem is part of this scope change.

## 검수 저장소 (B, `src/review/`, `reviews/`)
질문별 제도 판정(good/partial/bad/unsure)·답변 메모·제도별 주석·지침을 **추가만 하는 이벤트**로 `reviews/events/<검수자>.jsonl`에 쌓는다(사람마다 파일이 달라 git 충돌 없음). 판정은 `(질문, 제도, 제도 내용 버전)` 단위이고 버전은 서비스명·지원대상·선정기준·지원내용·신청방법·신청기한의 해시라, 데이터가 바뀌면 바뀐 것만 다시 검수한다. 기한이 지난 제도는 내보내기·대기열에서 자동 제외한다(판정은 보존). `export`가 `eval`용 시험지(dev/test 8:2, 기존 36문항은 dev, test는 덮어쓰기 금지)를 만들며 `relevant_service_ids`를 손으로 채우지 않는다. 앱 검수 모드(C)는 `ReviewStore.add_*`로 쓴다. 실사용 질문은 쓰지 않는다. 필드·규칙: `docs/REVIEW_STORE.md`. 검수 화면은 `python -m app.review_mode`(`app/review_mode.py` + `src/review/session.py`, `app/main.py`와 독립). `import-questions`로 csv/jsonl의 새 질문을 일괄 등록하고(중복 건너뜀), `eval.run_eval`은 `relevance`가 있는 시험지에서 Precision@K·nDCG@K·bad_rate·unjudged_rate도 출력한다.

## 공용 데이터 (데이터 팩, 2026-10-27)
DB(`data/services.db`)와 검색 색인(`data/vectorstore`)은 git에 없고 사람마다 다르면 같은 코드에서도 결과가 달라진다. 데이터 갱신(수집·색인·`catalog apply`)은 조장 한 명이 하고, `python -m src.datapack build`로 만든 팩 zip을 공유 드라이브에 올려 `data_pack/LATEST.json`만 커밋한다. 팀원은 `git pull` 후 `status`·`install`로 맞춘다(해시 검증, 임베딩 모델 불일치 차단, 기존 데이터는 `data/a_validation/datapack/_backup_*`에 백업). 각자 따로 수집·색인해 데이터를 갈라놓지 않는다. 팩에는 DB·DB↔색인 연결 기록·색인만 들어가고 `data/raw/`·`.env`·임베딩 캐시는 들어가지 않는다. 절차·주의: `docs/DATA_PACK.md`.

## Metadata/replay corrections (2026-10-22)

Shared additions are optional `Metadata.region_provenance` and `common.regions`; no required
Chunk constructor changes. B codec/filters preserve provenance and aliases while keeping
weights, penalties, field boosting and relaxation. C only labels provenance and prioritizes
explicit residence evidence over agency inference; full-service evidence expansion is unchanged.
`metadata_tools` traces without initializing collections/models; correction requires explicit
selected IDs and applies stored snapshots through the existing verified consumer. `--service-ids`
limits handoff; `--verify-every` guarantees remain. Normal replacement uses B's passage-embedding path, including its existing optional cache.
Replay uses `--offset/--limit` plus named DB checkpoints bound to source/DB/policy/parser/date;
`ingest_state` progress commits with services/pending/run summary. Exact commands and known local
path/region discrepancies are in `docs/INGEST_A.md`. Never infer index build revision from today's HEAD.

## Official decision documents A-1 (2026-10-23)

`document_catalog.py`, `document_acquire.py`, `official_documents.py` and `official_sources.json`
own isolated registration/original acquisition, byte-based versions, temporal/source facts,
audit and explicit topic-specific service links. Runtime catalog/originals/diagnostics have
separate explicit paths and never initialize the live API DB/index. pypdf validates structure/
page count only; no extraction/OCR/document chunks/index/events/retrieval/answers in A-1.
The existing API Chunk/Citation, pending/ack, locks/associations, B ranking and C expansion stay
unchanged. Separate document collection/events/citations are proposed, not agreed/implemented.
Commands, verified sources, unresolved currency and A-2/A-3 evaluation plan:
`docs/OFFICIAL_DOCUMENTS_A1.md`. No label changes or automatic human-review approval.

## 답변 생성 규칙 (사전기획서 6장·8장 기준)
- **실제 적합성 점검(`fit_check.py`)**: 판정 통과 후보를 답변 직전에 한 번 더 읽어 이미 이용 중인 제도와 같은 혜택·전제 조건(duplicate), 특정 직무·업종 전용(niche), 이름만 겹침(mismatch)만 뺀다. **빼는 모든 판정에는 자료에서 글자 그대로 복사한 근거 구절을 요구하고 코드가 대조한다**(duplicate는 이용 중인 제도 설명, 나머지는 후보 원문). 근거를 못 대면 판정을 버리고 제도를 둔다. 서로 다른 혜택은 병행 가능으로 보고 같은 분야라는 이유로 빼지 않는다. 안내가 5개 미만이면 다음 후보를 한 번 더 판정해 채운다(첫 묶음이 모두 빠진 질문은 근거 없음 유지).
- "내 조건으로 받을 수 있는 지원"(`benefit_tiers.py`)은 원문에 근거가 있는 항목과 수집 자료 밖 일반 정보를 구분해, 후자는 항상 "자료 밖 정보(정확하지 않을 수 있어요)"로 표시한다 (자료 밖 정보 사용은 팀 결정).
- 검색된 근거 안에서만 답한다. 근거가 없으면 모른다고 답하고 담당 기관·공식 사이트 확인을 안내한다.
- 자격이 불명확하거나 정형 조건 값이 비어 있으면 단정하지 않고 "확인 필요"로 표시한다.
- 모든 답변에 서비스명·소관기관·근거 문구·상세 링크를 붙이고, "최종 신청 자격은 담당 기관에서 확인" 안내를 넣는다.
- 신청기한이 지난 제도는 후보에서 제외한다. 정형 필터는 하드가 아니라 소프트로 적용하고, 결과가 없으면 필터를 풀어 다시 검색한다.
- 사용자에게 보이는 문구는 한국어로 작성한다.
- 판정 기준 문서는 해석·설명용이다. 금액·기한·신청 기간은 API 값을 쓰고, 문서와 다르면 API 값과 "담당 기관 확인"을 함께 표시한다.
- 판정 문서 근거가 있어도 자격을 단정하지 않는다("해당될 수 있음" + 근거 조항 + 최종 확인 안내). 판정 근거에는 문서명·발행 기관·연도·쪽·근거 문구를 붙인다.
- 사용자 지역과 다른 지역 문서는 근거로 쓰지 않는다. 개정 예고(행정예고) 내용은 "개정 예고"로 구분해 확정처럼 말하지 않는다.

## 평가 (사전기획서 9장)
- 검색: Hit Rate@K, Recall@K, MRR / 답변: Answer Correctness, Faithfulness, Citation Accuracy
- 실험은 Baseline(SQL+키워드) → Vector → Hybrid+정형 필터 → 최종 순서로 9.4에 기록한다.

## 작업 규칙
- 자기 담당 디렉터리 밖의 파일을 고칠 때는 담당자와 먼저 합의한다.
- 변경 전에 `docs/사전기획서.docx`의 해당 절을 확인하고, 설계가 바뀌면 기획서(8.3)에도 기록한다.
- 새 라이브러리를 추가하면 `requirements.txt`를 함께 갱신한다.
- **문서 동기화(자동)**: 디렉터리·모듈·함수 시그니처·데이터 계약(`src/common/schema.py`)·담당 영역·명령을 바꾸는 작업을 하면, 같은 커밋에서 `README.md`(팀 안내), `docs/ARCHITECTURE.md`(구조와 이유·변경 이력)와 이 파일의 해당 절을 함께 갱신한다. 문서만 빠진 커밋을 만들지 않는다.
- 새 기능은 새 파일로 만들고 다른 담당의 파일은 최대한 고치지 않는다. 고쳐야 하면 고치기 전에 담당자에게 알리고 승인받는다(팀 채팅 양식: 파일·요청자·담당·이유·변경 내용). 담당자가 직접 고치는 것을 우선하고, 커밋 메시지에 `[B파일]`처럼 표시하고 승인 여부를 적는다.
- **AI 작업 규칙**: AI(Claude 등)가 사용자의 담당 디렉터리 밖 파일이나 공유 파일(`src/common/`, `requirements.txt`, `.gitignore`, `tests/conftest.py`, `eval/questions.jsonl`, README·ARCHITECTURE·CLAUDE.md)을 고쳐야 하면, 고치기 전에 사용자에게 파일·이유·변경 내용을 알리고 승인을 기다린다.

## 자주 쓰는 명령
```
pip install -r requirements.txt        # 설치 (.env.example을 .env로 복사 후 키 입력)
python -m src.ingest.pipeline          # A: 실제 API 수집 (실행 전 승인 필요), 기본 증분
python -m src.ingest.pipeline --full   # A: 활성 조각 재생성, DB 초기화 없음
python -m src.ingest.pipeline --offline-fixture tests/fixtures/a_synthetic_api.json  # 격리된 합성 실행
python -m src.ingest.pipeline --validation --sample-pages 1 --per-page 3  # 승인 범위 내 격리 live 표본
python -m pytest tests/ingest -q -ra   # A: mock HTTP·임시 SQLite, 미구현 skip 금지
python -m src.ingest.handoff --dry-run # inspect pending; no index/model initialization
python -m src.ingest.handoff --apply   # cached model only, apply→verify→exact ack; stops on first failure (--verify-every 100: 100건씩 묶어 검증·확인, 대량 이벤트용)
python -m src.index.embed_shard run --shard 0/3 --out data/embed_shards/part0.npz   # B: 임베딩 계산 분담 (merge·coverage 하위 명령, 주 PC는 EMBEDDING_CACHE=data/embed_shards 로 handoff --apply)
python -m src.index.vector_store --backfill-region --dry-run   # B: 이미 색인된 조각의 비어 있는 region을 기관명으로 채우기 (--dry-run 없이 실행하면 적용, 임베딩은 그대로)
python -m src.index.vector_store       # legacy direct B CLI; bypasses consumer locking/ack
python -m src.datapack status          # 팀 공용 데이터 팩(DB·색인)이 최신인지 확인. 조장은 build로 만들고(`--out` 폴더, `--by`) data_pack/LATEST.json을 커밋, 팀원은 git pull 후 install (docs/DATA_PACK.md)
python -m app.main                     # C: 앱 실행
python -m eval.rag_compare             # C: 일반 LLM(검색 없음) vs 우리 서비스, 같은 채점기로 비교 (발표용)
python -m eval.rag_compare --demo      # C: 시연 후보(eval/demo_questions.jsonl) 두 답을 채점 없이 나란히 .md로
python -m app.catalog_mode             # 제도 목록 관리 웹 화면 (추가·수정·숨김·삭제). 숨김은 답변에 바로, 추가·수정은 python -m src.catalog apply 후 반영 (docs/CATALOG.md)
python -m src.generate.llm_cache stats # LLM 응답 저장소 현황 (같은 입력이면 같은 응답. 새 응답을 받으려면 LLM_CACHE=0, 공유 파일 갱신은 한 사람이 warm --fresh)
python -m app.review_mode              # B: 검수 화면 (후보 판정·답변 메모 -> reviews/events/)
python -m src.generate.explain "질문"   # 답변 선정 과정을 터미널에 출력 (제도를 고른·뺀 이유). 앱에서는 RAG_TRACE=1 환경변수로 같은 추적을 켠다 (색인·OPENAI_API_KEY 필요)
python -m pytest -rs                   # 테스트 (미구현은 skip. tests/fixtures 샘플로 A 없이 B, B 없이 C 검증 가능)
python -m eval.label_helper label --only q01,q02   # 평가 질문 정답 채우기 (사용법: docs/LABELING.md. fetch→label→check)
python -m eval.label_helper review                 # Claude 1차 정답의 사람 검수 대기 목록 (--ok q01,q02 로 완료 표시)
python -m src.review stats            # B: 검수 저장소 (reviews/, 사람별 이벤트 파일). import-legacy·import-questions·validate·agreement·export·queue 하위 명령, 설계는 docs/REVIEW_STORE.md
python -m eval.sweep                  # B: Hybrid 가중치·RRF_K·FIELD_BOOST·지역 감점(--local-penalty) 격자 탐색 (색인 필요, 검색 결과는 질문별 캐시)
python -m eval.run_eval --experiment hybrid_filter --k 5   # 검색 평가 (baseline|vector|bm25|hybrid|hybrid_filter|all, all은 비교표 출력). 정답이 채워진 질문 세트와 색인 필요 (baseline은 A의 SQLite `data/services.db` 필요)
python -m src.ingest.guides --dir data/guides   # A: 판정 기준 문서 PDF 파싱·분할 (제안)
python -m src.index.guide_store                 # B: 판정 기준 문서 색인 (제안)
```
Handoff settings: `--db-path`, `--index-dir`, `--model`; `--validation` requires explicit isolated paths and rejects live destinations/aliases before writes. Fixture/validation source runs require validation mode. One source DB ↔ one target/model is recorded in `<db>.handoff.json` and `<index>/handoff_manifest.json`; changing an association is not an automatic migration. Existing nonempty unbound indexes require `--adopt-existing`; verify the selected model is the original model, since an old collection slug alone cannot prove its identity. Model dimension/max sequence length/cosine settings must agree. Worker-only config changes and offline Hugging Face flags prevent caller cache/config leakage and model downloads. Raw data, DB/Chroma files, manifests and locks stay out of Git.

Current verification boundary: A/B/C are implemented; narrow residence-text extraction, Korean deadlines and durable segmented replay are tested offline; stored-event integration is tested using real Chroma and fake embeddings. Full API compatibility, production age/region adapters, actual-model verification and search/answer quality evaluation remain separate work. The previous live smoke record is historical, not a live run in this handoff task.

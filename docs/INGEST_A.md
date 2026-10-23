# A 수집·정제·SQLite·분할 운영 계약

2026-10-19 구현 및 2차 수정. `feat/ingest-a-v1` 체크아웃의 공개 함수·반환형과 `Chunk`를 유지한다.
The prior second pass recorded three offline corrections and a small isolated live sample.
The 2026-10-20 handoff pass adds real Chroma/BM25 integration using fake embeddings.
No new live API, actual-model or production-data validation is claimed.

## API 확인 수준과 필드 매핑

공식 출처: [공공데이터포털 제공기관 페이지](https://www.data.go.kr/data/15113968/openapi.do).
목록·상세 제공, JSON/XML, 실시간 갱신 및 인증/호출 제한 오류 설명은 **문서 확인**이다.
1차 작업에서는 상세 OAS와 라이브를 확인하지 못했다. 2차 작업에서 포털 HTML이 연결하는
[공식 OAS](https://infuser.odcloud.kr/api/stages/44436/api-docs?1684891964110)를 확보하고,
세 엔드포인트의 첫 페이지를 각각 3건씩 실제 관측했다. 아래 확인 수준은 이 작은 표본에 한정된다.
관측 시각은 원천 수정 시각이 아니다. OAS의 수정일시 설명도 비어 있어 필드별 최신성 순서를 보장하지 않는다.

| 엔드포인트 / 후보 원천 필드 | 정규 필드 | 결합 키·예상 카디널리티 | 원천 누락 처리 | 확인 수준 |
|---|---|---|---|---|
| 세 엔드포인트 / 서비스ID | service_id | 표본 3개 ID 모두 1:1 결합 | 비어 있거나 중복 충돌이면 보류 | 문서·표본 확인, 전체 카디널리티 미검증 |
| serviceList, serviceDetail / 서비스명 | service_name | 동일 ID | 선택 결과 이름이 비면 갱신 보류 | 문서·표본 확인 |
| serviceList, serviceDetail / 지원대상, 선정기준 | support_target, selection_criteria | 동일 ID | 현재 상세의 빈 값·누락은 제거, 상세 실패 시 아래 병합 규칙 | 문서·표본 확인 |
| serviceList, serviceDetail / 지원내용, 신청방법 | support_content, how_to_apply | 동일 ID | 위와 동일 | 문서·표본 확인 |
| serviceList, serviceDetail / 신청기한 | application_period, deadline | 동일 ID | 원문 보존, 명확한 날짜만 ISO 종료일 | 문서·표본 확인 |
| serviceList, serviceDetail / 소관기관명 | agency | 동일 ID | 명시적 빈 값 유지, metadata 생략 | 문서·표본 확인 |
| serviceList / 서비스분야 | field | 동일 ID | 미상 키 생략 | 문서·표본 확인, 상세에는 관측되지 않음 |
| serviceList / 상세조회URL | detail_url | 동일 ID | 생성하지 않음, HTTPS 정부 도메인·품질 진단 | 문서·표본 확인, 상세에는 관측되지 않음 |
| supportConditions / 서비스ID, JA* | `_sources.supportConditions` | 동일 ID | 실패/미관측이면 기존 값 유지 | 필드 관측·기본 라벨 문서 확인, 실제 자격 어댑터 보류 |
| 검증된 A 어댑터 / `_verified_eligibility` | eligibility, metadata | API 원본 필드가 아닌 A 내부 계약 | 미검증 JA* 코드를 자동 변환하지 않음 | 합성 테스트만 확인 |
| 각 엔드포인트 / page, perPage, currentCount, totalCount, matchCount | Collection, 실행 요약 | 엔드포인트별 독립 페이지 | 누락·불일치·반복은 실패/부분 수집 | OAS 정수 타입·첫 페이지 확인, 전체 종료·일관성 미검증 |

`serviceDetail`과 `supportConditions` 모두 OAS에 page/perPage 및 선택적 `cond[서비스ID::EQ]`가 있다.
현재는 페이지 방식으로 수집하며 ID 필터 질의는 실측하지 않았다. ID는 공백 없는 비어 있지 않은 문자열만 허용한다.
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

`client.VERIFIED_CONTRACTS`는 세 엔드포인트 모두 False를 유지한다. 첫 페이지 확인만으로 전체 순회 종료,
빈 스냅샷 및 권위 있는 전체 목록의 안전성을 입증할 수 없다. CLI에 검증 우회 옵션은 없다.
`mark_closed()`는 인자 없는 기존 인터페이스에서 안전하게 `[]`를 반환한다. 파이프라인의 A 내부
`apply_run()`만 **검증된·권위 있는·비표본·비어 있지 않은 완전 목록**을 받아 트랜잭션 안에서
소실을 판정한다. dataset inclusion 전 전체 목록 ID와 비교하며 상세/조건 누락으로 소실을 판단하지 않는다.
최초 전체 0건의 권위성을 확인할 정책은 아직 없으므로 자동 전체 폐쇄를 지원하지 않는다.

## Normalization, observations and dataset inclusion

`merge.py`에서 키 존재 여부와 관측 상태로 필드를 결정하며 truthiness로 누락/빈 값을 합치지 않는다.

| 상황 | 결정 |
|---|---|
| 현재 상세 성공 행의 네 본문 항목 | 상세 우선. 해당 키 누락·null·빈 값은 정상 관측의 제거 |
| 현재 상세 실패·행 누락 | 현재 목록의 본문을 사용하고, 그 필드가 목록에도 없으면 과거 정상 본문 보존 |
| 이름·기관·분야·URL: 현재 의미 있는 값 존재 | 현재 값 우선. 두 현재 값이 충돌하면 목록 우선, 충돌 기록 |
| 기한: 두 현재의 의미 있는 값 충돌 | 둘 다 명확한 날짜면 더 이른 종료일, 하나라도 모호하면 모호한 기간 선택·확인 필요 기록 |
| 현재 값은 명시적으로 비고 다른 현재 유효 값도 없음 | 과거 값으로 채우지 않고 제거. 이름이 비면 identity 요건 때문에 해당 갱신 보류 |
| 두 현재 원천 모두 해당 공통 필드 키가 없음 | 직전 필드별 결정값(명시적 삭제 포함) → 과거 상세 → 과거 목록 순으로 fallback |

과거 상세 기한 2026-10-18, 현재 목록 기한 2027-12-31, 현재 상세 실패이면 본문은 보존하고 기한은
2027-12-31이다. 두 현재 원천의 충돌 규칙은 원천 갱신 순서가 확인되지 않은 명시적 잠정 규칙이다.
현재 값이 반드시 더 최근에 수정됐다는 뜻이 아니며, 서로 다른 기간·기관 등이 있으면 확인이 필요하다.

`_sources`는 마지막 성공 원본, `_observations`는 원천별 fresh/retained/unavailable이다.
`_field_provenance`는 선택 원천·값·이유와 입력별 fresh_value/fresh_empty/fresh_missing/fresh_invalid,
retained_value/empty/missing/invalid 및 unavailable을 구분한다. `_merge_conflicts`는 필드명,
후보 원천/관측 상태/값, 선택값과 이유를 담는다. 실행별 `merge-conflicts.json`에도 모든 충돌을 기록한다.
실행 요약에는 충돌 수와 대표 사례를 넣는다. 감사 정보는 Chunk에 추가하지 않고 내용 해시에서 제외한다.
조건 수집 실패는 이전 정상 조건을 유지한다.

필드별 선택 원문도 A 감사 레코드에 보존해, 이후 목록에서 키가 반복 누락돼도 연장 기한이나 명시적
삭제가 더 오래된 상세 값으로 되돌아가지 않게 한다. 원천별 마지막 성공 행 자체를 수정해 채우지는 않는다.

개행·숫자·단위·부정·예외·목록을 보존하는 규칙 정제만 수행한다. LLM을 호출하지 않는다.
Whole numeric/Korean dates and supported ~ ranges are parsed (strict grammar below). `상시`, `예산 소진 시`, `별도 공고`, 혼합/모호한 기간은
원문만 보존한다. UTC 시스템 날짜와 무관하게 만료 기준일은 한국 시간이다.

OAS에서 JA0110/JA0111의 대상연령 시작/종료 라벨은 확인했다. 그러나 신청자/부양자 적용,
경계 포함 여부·null/특수값·다중 조건까지 검증한 생산 어댑터는 없어 자동 변환을 보류한다.
본문의 자녀 나이·복합 조건을 단일 신청자 나이로 추출하지 않는다. 내부 어댑터 계약은 다음과 같다(합성 테스트용):

```python
conditions = {"서비스ID": "A", "_verified_eligibility": {
    "source": "applicant", "age_ranges": [[19, 34]], "region": "전국"
}}
service = clean_service(raw, conditions)
```

이 객체는 실제 API 필드가 아니다. 다중 연령 구간은 eligibility에 보존하지만 공유 Metadata의 단일
age_min/age_max로 평탄화하지 않는다. 명시적인 전국은 `region='전국'`, 미상은 키 생략이다.
A does not infer residence eligibility from agency names. A accepts narrowly scoped explicit residence statements; structured age-code mappings remain deferred. 모든 미상 metadata 키는 생략한다.
기관/링크가 없으면 data_quality와 실행별 품질 건수로 드러내며 거짓 출처를 만들지 않는다.

`is_youth_service()` keeps its legacy boolean youth-tag interface. Dataset inclusion now
uses `policy.inclusion_decision()` / `is_included_service()`, policy `personal-household-v1`.
General personal/household and dependent benefits are included with supported reasons;
uncertain applicability is `provisional`. Only explicit institution-only target assertions
(법인만, 공공기관만, 지방자치단체만, 비영리단체만, and equivalent narrow forms)
support exclusion, and contradictory personal recipient evidence prevents that exclusion.
Business wording or age alone is insufficient. Inclusion reasons, uncertainty and policy ID
are stored in existing payloads; run summaries count outcomes. This is not eligibility.

New exclusions use `policy_scope_exit`; old `youth_scope_exit` audit records are retained.
Minimal excluded records lack sections and cannot be restored by requeue alone. Failed
auxiliary collection remains distinct from source missingness; missing observations do
not justify scope removal or unsupported reopening. Per-record `data_quality` remains.

## Historical raw reprocessing (원본 재처리)

`reprocess.reprocess(source_dir, storage=..., limit=100, today=...)` and its CLI replay ONE
original A acquisition directory: `run-summary.json`, matching endpoint summaries and
sequential `serviceList-00001.json` / `serviceDetail-...` / `supportConditions-...` pages.
Decoded counts/pages/totals/status/conflicting IDs must agree; API-error pages are rejected.
Joins use service IDs, never page positions. `label_helper`'s standalone `serviceList.json`
is not this contract. Unmanifested endpoints or inconsistent layouts fail before DB mutation.
Missing details defer the record; missing conditions defer restoration of a minimal excluded
record. New records with details and missing conditions can retain unknown eligibility.

The limit selects [offset, offset+limit) in source order (1–1000 records); decoding validates the
entire selected acquisition run. No runs are mixed. Prior stored auxiliary fallback can be
retained only after conservative record-level observation ordering passes. Original acquisition
run/time and raw-file SHA-256 values are retained in payload/run audit. Replay field states
are `historical_*`, source states are historical/retained/unavailable, never fresh.
Run-ID acquisition time is not the provider's update time. Older/equal different-source or
unknown-order observations defer the entire record. The same acquisition may be replayed
idempotently; API-disappeared records require a new live observation to reopen. Missing
source data is explicit deferral, not fabricated evidence. Deferred IDs/reasons are durable.

Policy transitions compare status/reason as well as content hash: inactive legacy records
can create a full replacement even when their hash is unchanged. Identical replay creates
no new version/event. Replay holds the same source OS lock and commits records/events/run
summary transactionally; it never resets DB/history/pending. Every replay is sampled and
non-authoritative with disappearance disabled. Absence from the selected run proves nothing.

`pipeline --full` still performs collection and requeues ALL current active chunks, preserving
pending removals/history. It is neither an offline import nor a recovery mechanism for discarded
sections. Raw reprocessing is separate and does not apply or acknowledge any index events.

```powershell
$scopePython = ".\.venv\Scripts\python.exe"
$scopeRoot = "data/a_validation/scope-local"
# Choose an existing original A acquisition run, not a label_helper list.
& $scopePython -m src.ingest.reprocess --source-run data/a_validation/readiness-real-20261020/raw/20261020T061626Z-72e54f11711f --db-path "$scopeRoot/services.db" --raw-dir "$scopeRoot/replay-raw" --limit 50 --today 2026-10-21 --validation
& $scopePython -m src.ingest.handoff --db-path "$scopeRoot/services.db" --index-dir "$scopeRoot/vectorstore" --validation --dry-run
# Explicit application only when requested; cached compatible model, existing association rules.
& $scopePython -m src.ingest.handoff --db-path "$scopeRoot/services.db" --index-dir "$scopeRoot/vectorstore" --validation --apply
& $scopePython -m src.ingest.coverage --db-path "$scopeRoot/services.db" --index-dir "$scopeRoot/vectorstore" --source-run data/a_validation/readiness-real-20261020/raw/20261020T061626Z-72e54f11711f --only q15,q16,q23
```

Coverage reuses B's `missing_gold` ID inspection and reads SQLite/Chroma metadata in read-only
transactions without initializing a collection/model, ingestion or acknowledgment. It shows
effective paths, DB status, deferral, pending and exact persisted snapshot consistency. Unsupported
Chroma schemas fail clearly. Source and index snapshots are separate; inspect after writers stop.
Absence in a sample is not an invalid gold label. Index presence does not establish geographic
applicability, applicant eligibility or retrieval quality. B's region fallback is checked through
the current codec; A residence eligibility remains unknown. B ranking/Hybrid/field boost is unchanged.

## Integrated metadata and resumable replay (2026-10-22)

### Region meaning and provenance

`common.regions.normalize_region()` uses the existing B/C province/metropolitan vocabulary,
including historical 강원도/전라북도 and current special-province aliases. 서울특별시/서울시/서울
become 서울. Unrecognized names never become 전국; explicit 전국 is distinct from unknown.
Python and Chroma filters normalize user aliases consistently and use the existing agency
fallback when A provides no supported single region. Unknown still passes the soft filter.
Hybrid weights, local penalties, field boosting and relaxation defaults are unchanged.

The A `regions.py` adapter accepts only anchored residence statements in 지원대상/선정기준:
for example `서울특별시에 거주하는 청년` or `경기도 용인시 거주자`. It does not search arbitrary
place mentions or derive applicant residence from agency/office/school location. Dependent,
negative, alternative and ambiguous compound statements remain unresolved. Multiple source
locations are preserved in `eligibility.residence_evidence`; different provinces have no single
A region. District-qualified statements retain the full location and a
`district_limited_province_approximation` label. A shared province field does not express complete
district eligibility. An agency fallback may still influence retrieval when a single A region
cannot be represented; that is visibly heuristic, not evidence that all source alternatives match.

Evidence records include endpoint, normalized/source field, source phrase, raw/canonical region,
rule and observation/acquisition provenance when known. Clear current residence evidence supersedes untagged legacy regions while preserving the old value in A audit. `clean_service()` alone cannot know a
merged endpoint; `_join()` resolves that provenance. Source observation/run IDs do not enter the
semantic content hash. Existing nonempty untagged values are `legacy_unknown`, not verified.

Optional `Metadata.region_provenance` is preserved by Chunk serialization, pending validation,
B encoding/Chroma/decoding, exact handoff verification and C evidence metadata:

| Value | Meaning |
|---|---|
| `explicit_eligibility` | Supported explicit residence evidence or the existing explicitly verified internal applicant adapter; not a claim of full district/compound eligibility |
| `agency_inference` | B's existing agency-name retrieval heuristic |
| `legacy_unknown` | Nonempty legacy region with unavailable provenance |
| `unknown` | No supported region |

A explicit region takes precedence over agency inference. Legacy documents remain readable;
changing code does not change persisted documents. C's evidence prompt carries provenance and
its regional postprocessing prefers explicit A evidence over agency inference. Existing soft
relaxation remains: C can restore candidates if all were removed. Postprocessing cannot recover
candidate positions already occupied by other-region results, and does not always remove every
mismatch. No answer-quality improvement is inferred from metadata correctness alone.

### Diagnosis and correction boundaries

`metadata_tools trace` reads only SQLite/Chroma metadata, without PersistentClient, HNSW/model
initialization, API calls, index-version writes or acknowledgment. It shows effective DB/index/
collection/model/manifest paths, current revision/dirty state and selected source→payload→Chunk
→pending→expected B codec→persisted→decoded metadata. A standalone label-helper listing is
diagnostic evidence only, not an A replay acquisition. Unsupported schema or inaccessible
selected resources are reported explicitly; another collection/default is never substituted.
The output distinguishes service and chunk counts. Missing region is not vector corruption.

Observed locally on 2026-10-22: selected `data/vectorstore`, collection `chunks__BAAI_bge-m3`,
33,021 chunks / 10,603 services; 9,522 blank-region chunks (28.84%), 2,817 services with blank
chunks. Of the blanks, 1,828 chunks have a current agency fallback. Actual examples include
`405000000631` (경기도 용인시 → 경기) and `330000000110` (부산광역시 동래구 → 부산).
Blank central-agency cases such as `116010000001` remain unknown, not confirmed nationwide.
These counts are one local index inspection, not an API population or newly established error rate.

The selected default `data/services.db` was missing. The index manifest names another absolute
workspace path, and records no build revision. Hence the blank-versus-current-code mismatch and
path association discrepancy are observed; old code, alternate writers or incomplete updates
remain hypotheses. Today's Git HEAD is not the historical build revision. Restore/identify the
correct source/index association before production correction; do not edit manifests by hand.

`metadata_tools correct` is dry-run by default. Explicit `--apply --service-ids ...` (1–50 IDs)
calls `store.reconcile_services()` under the existing source lock. The producer re-derives
metadata/deadlines from CURRENT authoritative normalized records, preserving acquisition
history. Changed hash/status creates normal events; codec-only changes can explicitly requeue
a new service version. Equivalent existing pending snapshots are reused. Inactive/missing
records are skipped, never resurrected from an index copy. All current service chunks are
replaced; empty replacements/removals retain existing semantics. The consumer applies ONLY
the stored selected events, verifies persistent/Vector/BM25 state and acknowledges exact tokens.
Unrelated pending is preserved. `--verify-every` batch failure/replay semantics and one
source/index/model association remain intact. Default handoff without selection is unchanged.

Metadata-only embedding reuse is **not implemented**. Normal replacement uses B's passage
embedding path and may also use query embeddings for verification. No unsafe metadata patch,
collection clearing, model download, or production-wide backfill is implicit.

### Korean deadline grammar

`parse_deadline()` still returns ISO date or None. Grammar: a whole numeric `YYYY-MM-DD`,
`YYYY/MM/DD`, `YYYY.MM.DD` (optional final period), or `YYYY년 M월 D일` (spaces optional),
optionally followed by `~`/`～` and another whole supported date. Calendar validation and
nondecreasing range order are required; a range returns its ending date. No arbitrary date
extraction or current-year inference occurs. 상시/예산 소진 시/별도 공고/multiple rounds and
unsupported conditional schedules remain unparsed, with original application-period text kept.
Only reliably parsed deadlines authorize automatic expiry. Deadline day is inclusive under
the existing Asia/Seoul reference-date contract. Unknown is not confirmed open.

### Segments and durable resume

`--offset` is zero-based in validated page/row first-occurrence order of accepted unique IDs; `--limit` remains 1–1000
and defaults to 100, offset defaults to 0. Conflicting listing IDs are outside this valid order and remain explicitly unresolved in the checkpoint; raw endpoint counts remain separate. The entire chosen acquisition is validated before
selection. Replay reports source total, selected half-open interval, attempted/processed/
unchanged/deferred counts, next position and end-of-source. End-of-source does not mean all
records were restored or indexed. Deferred IDs/positions/reasons remain separately identifiable.

`--checkpoint NAME` starts a new named cursor; `--resume NAME` continues it. The small A-owned
`ingest_state(key,value)` table stores database UUID and cursor JSON. Binding covers original
run ID and SHA-256 of acquisition contents/summaries, ordering version, target DB UUID/path,
inclusion policy, normalization/adapter implementation fingerprint, chunk/tag settings, fixed
reference date and inclusive Asia/Seoul semantics. A changed source/parser/policy/target/date
rejects resume. A new pass uses a NEW checkpoint name and an explicit offset/date; it never
deletes the old checkpoint, resets data, or mutates old events. Do not omit `--today` on a later
day and assume a previous checkpoint will silently change reference dates.

Cursor progress, services, pending and completed run summary commit in one transaction.
Transaction failure does not advance. If raw `run-summary.json` output fails after commit,
the DB cursor remains authoritative and resume starts at its committed next position. A crash
before the first checkpoint commit requires rerunning the initial command; it cannot skip work.
Supported writers use the same cross-process source lock. Historical ordering/deferrals remain,
and replay never authorizes disappearance or changes global verified/authoritative gates.

```powershell
$metaPython = ".\.venv\Scripts\python.exe"
$metaRoot = "data/a_validation/metadata-replay-local"
$metaSource = "data/a_validation/readiness-real-20261020/raw/20261020T061626Z-72e54f11711f"

# Read-only actual selected index. Missing source DB is reported, never replaced by another default.
& $metaPython -m src.ingest.metadata_tools trace --db-path data/services.db --index-dir data/vectorstore --model BAAI/bge-m3 --service-ids 405000000631 330000000110 116010000001 --raw-list data/raw/serviceList.json

# Bounded local replay: [0,3), then resume [3,6), then an explicit later interval [50,52).
& $metaPython -m src.ingest.reprocess --source-run $metaSource --db-path "$metaRoot/services.db" --raw-dir "$metaRoot/replay-raw" --offset 0 --limit 3 --checkpoint small --today 2026-10-22 --validation
& $metaPython -m src.ingest.reprocess --source-run $metaSource --db-path "$metaRoot/services.db" --raw-dir "$metaRoot/replay-raw" --limit 3 --resume small --today 2026-10-22 --validation
& $metaPython -m src.ingest.reprocess --source-run $metaSource --db-path "$metaRoot/services.db" --raw-dir "$metaRoot/replay-raw" --offset 50 --limit 2 --today 2026-10-22 --validation

# Inspect correction; produces no event/index/model initialization or acknowledgment.
& $metaPython -m src.ingest.metadata_tools correct --db-path "$metaRoot/services.db" --index-dir "$metaRoot/vectorstore" --service-ids 105100000001 --today 2026-10-22 --validation --dry-run
& $metaPython -m src.ingest.handoff --db-path "$metaRoot/services.db" --index-dir "$metaRoot/vectorstore" --service-ids 105100000001 --validation --dry-run

# Explicit application only: cached compatible model and valid source/index association required.
& $metaPython -m src.ingest.metadata_tools correct --db-path "$metaRoot/services.db" --index-dir "$metaRoot/vectorstore" --model BAAI/bge-m3 --service-ids 105100000001 --verify-every 2 --today 2026-10-22 --validation --apply
# Retry failed selected events without re-deriving an older stored event:
& $metaPython -m src.ingest.handoff --db-path "$metaRoot/services.db" --index-dir "$metaRoot/vectorstore" --model BAAI/bge-m3 --service-ids 105100000001 --verify-every 2 --validation --apply
```

For read-only checkpoint inspection:

```python
from pathlib import Path
from src.ingest import store
from src.ingest.storage import StoragePaths
paths = StoragePaths(Path("data/a_validation/metadata-replay-local/services.db"),
                     Path("data/a_validation/metadata-replay-local/replay-raw"), "validation")
state = store.read_state("replay:small", storage=paths)
```

The existing `--full` ingestion option still collects/requeues active services. It does not
recover sections discarded from excluded records or create a historical replay cursor.
Actual age-code semantics remain deferred: local OAS labels JA0110/JA0111 as age start/end,
but applicant/dependent meaning, inclusive boundaries, special/null/unrestricted values and
alternative ranges need independent provider evidence. `_verified_eligibility` is not a real
API mapping. Full traversal/disappearance authority remains unverified.

## SQLite·분할·증분 상태

일반 live 기본값은 기존 `config.DB_PATH`, `config.RAW_DIR`이며 표준 sqlite3를 쓴다.
`storage.StoragePaths`를 명시적으로 전달해 전역 config 변경 없이 실행별 DB/raw를 선택한다.
테이블은 services(정규 JSON·해시·상태·버전),
ingestion_runs(완전성 포함 요약), pending_changes(서비스별 최신 미확인 이벤트), ingest_state(DB identity/replay cursor) four tables.
상태/원인: active 또는 inactive, `expired`/`api_disappeared`/`policy_scope_exit` (legacy `youth_scope_exit` retained)를 구분한다.
본문·이름·기관·URL·기간 원문·검증 자격·metadata의 정렬 JSON SHA-256으로 변경을 감지한다.
수집 시각·fresh/retained 같은 운영 정보는 해시에서 제외한다. metadata만 바뀌어도 갱신된다.

DB 갱신과 pending 기록, 완료 실행 요약은 한 트랜잭션이다. 중간 실패는 롤백하고, 수집 중 중단은
다음 실행 시작 시 interrupted로 기록한다. 프로세스 파일 잠금으로 수집/upsert 중첩을 막고
프로세스 사망 시 OS가 잠금을 해제한다. DB 파일이나 이력을 초기화하지 않는다.

`load_services(None)`는 all active included services, `load_services([])`는 빈 목록이다.
`sql_keyword_search(keywords, k)`는 활성 레코드 이름·기관·네 본문 항목에 literal OR LIKE를 적용하며
%, _, 역슬래시를 escape한다. SQLite의 기본 LIKE 대소문자 규칙(ASCII case insensitive)을 따른다.
서비스 ID 순서와 최대 k를 사용하며 검색 순위·정형 필터 실험은 B 소유다.

SECTIONS 네 개만 분할한다. 각 text는 `서비스명 + '\n' + 본문`이다. 전체 text가 max_chars 이하다.
문단→줄→문장→고정 길이로 재귀 분할하고 구분자를 포함하여 모든 정규 본문을 보존한다.
빈 항목은 생략하고 `service_id#section`, 분할 시 `service_id#section-n`을 사용한다.
서비스명 접두 이후 본문 한 글자도 못 넣는 한도와 양의 정수가 아닌 한도는 ValueError다.

HTMLParser로 td/th의 셀 경계는 공백, tr/블록의 행 경계는 개행으로 보존한다. 셀 내부 br/블록,
중첩 인라인 태그, 엔티티·숫자·단위·부정·예외를 보존한다. 중첩 HTML을 정규식으로 제거하지 않는다.
정제는 clean_service에서 한 번 수행해 escaped literal markup의 이중 해석을 피한다.
이전 HTML 표에서 숫자가 붙었던 레코드는 다음 성공 관측 때 해시와 서비스 전체 조각이 한 번 갱신될 수 있다.
운영 DB 자동 마이그레이션이나 기존 raw 자동 재처리는 하지 않는다.

## Stored-event consumer (증분 색인 인계 소비자)

`pipeline.run(full=False) -> list[Chunk]` remains compatible and may replay pending
chunks even when the latest collection changed nothing. The supported index command
is `python -m src.ingest.handoff`; it consumes stored `pending_changes` snapshots,
never `pipeline.run()` output or current service records. It does not collect APIs.

| Event | Existing B operation | Completion |
|---|---|---|
| `replace`, nonempty complete `chunks` | `build_index(all_service_chunks)` removes stale IDs, then upserts | Verify → exact acknowledgment |
| `replace`, explicit `chunks=[]` | `remove_services([service_id])` | Verify all previous chunks absent → acknowledgment |
| `remove`, explicit `chunks=[]` | `remove_services([service_id])` | Same, preserving the removal reason in A state |
| Missing/null/malformed chunks, duplicate IDs, mismatched service, unsupported metadata | Reject before initializing the index | No acknowledgment |

Pending stores the latest event per service. Events are read in service ID order and
tokens are revalidated under locks before application. Acknowledgment matches the
original `service_id + version + event_id` and must return exactly 1. Reading,
serializing or successfully calling a B writer does not acknowledge anything.
`store.get_pending_changes()`, `pending_chunks()`, `pending_removal_ids()` and
`acknowledge_changes()` retain their interfaces. `handoff.read_pending(settings)`
adds read-only inspection without schema initialization.

### Verification, failure and replay

The A adapter compares persistent chunk IDs, text and B's encoded metadata with the
event, confirms obsolete chunks are absent, and compares unrelated documents before
and after. Service-scoped Vector queries request all event chunks, avoiding unrelated
top-K ranking. B's actual BM25 cache is prewarmed, then rebuilt by its version marker;
all corpus IDs/token frequencies must match persistent documents. Actual BM25 queries
must match B's positive-score semantics. Okapi can give nonpositive scores in tiny or
common-term corpora: membership/frequencies establish visibility in those cases;
the consumer does not alter B ranking to force a positive hit. Removals are proved by
persistent/corpus absence, together with empty service-scoped retrieval results.

Chroma stores source documents; BM25 remains derived in memory, reconstructed after
restart. No second persistent BM25 database is introduced. This is at-least-once
application, not a transaction across SQLite and Chroma. A crash after partial/full
index application and before acknowledgment leaves the event replayable. Retry the
same command after resolving the failure. Application stops at the first failed stage;
earlier verified events remain acknowledged, failed/later events remain pending.
The JSON report includes effective mode/paths/model, applied/acknowledged counts,
successful event tokens, failing stage/code/token, and remaining count. Failure exits 1.

### Batched verification (`--verify-every N`, B·A 합의로 추가)

The default (`N=1`) is unchanged: apply, verify and acknowledge one event at a time. With `--verify-every N`
(1–1000; recommended 100 for large queues) the worker applies N events, verifies them together against one
index snapshot and acknowledges exactly those N tokens. Why: per-event verification re-reads and re-tokenizes the
whole corpus, so total time grows with events × corpus size. Per batch the worker now: revalidates the pending
queue once (`read_pending` parses every pending event), takes ONE before/after snapshot, compares untouched
services once, and checks BM25 corpus membership/token frequencies once. Per event it still checks the exact
persistent chunk snapshot, Vector visibility of every chunk, and one bounded BM25 visibility query (the service-name
line; whole-chunk queries cost O(query tokens × corpus) each and dominated time).

Failure semantics are the same at-least-once model: the first failing stage stops the run; verified-and-
acknowledged batches stay done; the failing batch's events are NOT acknowledged (a superseded event keeps its
newer token pending while verified events with matching tokens are acknowledged) and replay by rerunning the same
command. A kill or Ctrl+C therefore redoes at most N events. The failure report adds `batch: {size, first_service_id}`.
`Settings.verify_every` is part of the worker settings; it does not affect the association manifests. Per-event
`BIndexAdapter.verify/capture` remain for N=1.

### Synchronization and association

The application worker takes OS locks in fixed order:

1. `<index-dir>.handoff.lock` protects the target directory across consumers/DBs.
2. `<source-db>.ingest.lock` protects the source events against supported ingestion.

Both are held through read/revalidation, application, verification and acknowledgment.
Acquisition fails immediately if busy (bounded wait: zero seconds). `pipeline._execute`
(including fixture CLI), `store.upsert_services()` and public `store.apply_run()` share
the source lock. The pipeline calls `_apply_run_locked()` only inside its existing
lock. Ingestion takes no target lock, so the order cannot deadlock. OS handles release
locks on failure/process death; an old lock file is not a lease. The worker owns locks
itself; terminating its launcher does not leave an unlocked, still-writing child.

One DB acknowledgment queue supports **one target directory/model configuration**.
`<db>.handoff.json` and `<index>/handoff_manifest.json` record resolved paths, exact
model ID, dimension, max sequence length, cosine distance and B passage/query policy.
Mismatch fails before writes to service chunks. An interrupted first binding can
recover a missing matching manifest half. Do not delete/edit manifests to repoint a
consumed queue or maintain multiple independent indexes; migration/rebuilding a lost
index requires a separately reviewed full-service regeneration/recovery procedure.

The first association of a nonempty legacy B collection requires `--adopt-existing`.
Confirm its original model/settings: an old model-name slug alone cannot prove exact
model identity. Existing collection name, dimension and cosine metric are checked;
no reset or alternate model download is attempted. Worker-only configuration keeps
caller config/client caches unchanged. Hugging Face offline flags require an already
cached matching model. This pass does not run or download an actual model.

The legacy `python -m src.index.vector_store` CLI and arbitrary direct B API/SQL calls
bypass these locks and exact-event acknowledgment. Do not run them concurrently with
the supported ingestion/consumer commands. Stale-ack protection alone cannot prevent
stale writes by multiple bypassing consumers. Online readers are not transactional
across SQLite/Chroma and may see an in-progress replacement until verification/replay
finishes; this protocol serializes writers, not all user queries.

### PowerShell commands

```powershell
# Default inspection: no index initialization, model loading, downloads or API calls.
.\.venv\Scripts\python.exe -m src.ingest.handoff --dry-run
# Apply with the configured DB/index and cached matching model.
.\.venv\Scripts\python.exe -m src.ingest.handoff --apply
# First association of an existing nonempty B collection only:
.\.venv\Scripts\python.exe -m src.ingest.handoff --apply --adopt-existing
# Replay after a failure: run the same --apply command again.

# Offline source: never use live DB/raw/vector storage. No API calls here.
.\.venv\Scripts\python.exe -m src.ingest.pipeline --offline-fixture tests/fixtures/a_synthetic_api.json --db-path data/a_validation/handoff-demo/services.db --raw-dir data/a_validation/handoff-demo/raw
.\.venv\Scripts\python.exe -m src.ingest.handoff --validation --db-path data/a_validation/handoff-demo/services.db --index-dir data/a_validation/handoff-demo/vectorstore --dry-run
# Requires an already cached matching model (actual model execution, not part of this task):
.\.venv\Scripts\python.exe -m src.ingest.handoff --validation --db-path data/a_validation/handoff-demo/services.db --index-dir data/a_validation/handoff-demo/vectorstore --model BAAI/bge-m3 --apply

# Isolated real Chroma + fake-embedding verification, no model/key/network required:
.\.venv\Scripts\python.exe -m pytest tests/ingest/test_handoff.py -q -rs --basetemp=data/a_validation/handoff-tests
```

`--validation` requires explicit DB/index paths and rejects resolved live DB/raw/index
destinations, aliases, sidecars, manifests and locks before writes. Existing offline/
test/validation ingestion history requires validation mode for application, even if a
later live run failed. Source DB hardlinks are rejected in all modes, because alias
paths would give one queue different locks/manifests. Normal
live defaults remain `config.DB_PATH` and `config.VECTORSTORE_DIR`. Use an ignored
`data/a_validation/<run>/` tree for all synthetic/validation DB/index/runtime artifacts.
Dry-run does not initialize a missing DB; run collection/fixture setup first.

### Preserved/deferred team decisions

`Chunk`, B storage/retrieval, interest field score boosting, C evidence expansion,
expiry/disappearance remain separate; dataset scope now follows personal-household-v1, with legacy youth-scope audit preserved.
Real Chroma/fake-embedding tests verify replay, replacement/removal, exact acknowledgment,
cross-process exclusion, worker isolation and C full-service expansion without an LLM.
They do not establish actual-model behavior, full API traversal or answer/search quality.

Missing agency/URL stays in `data_quality`; `Chunk` has no quality/provenance extension.
B/C can use `store.get_record(service_id, storage=...)` to inspect the same source DB.
Holding incomplete-provenance candidates or changing C display behavior remains a team
decision. Real age/region adapters still need verified JA/codebook semantics; internal
`_verified_eligibility` synthetic data does not implement a production mapping.

## 실행·테스트·일일 스케줄

PowerShell, 현재 workspace에서 실행한다. 실제 API 실행은 승인된 범위에서만 수행한다.

live는 기존 경로를 그대로 사용한다. `--offline-fixture`는 기본적으로
`data/a_validation/offline-<unique_id>/services.db`와 `raw/`를 자동 선택한다.
`--validation --sample-pages N`은 별도의 `validation-<unique_id>/`를 선택한다.
원하면 `--db-path`, `--raw-dir`로 격리 경로를 지정할 수 있다. fixture와 validation은 resolved 경로가
운영 DB·sidecar·lock 또는 운영 raw와 같거나 겹치면 디렉터리/쓰기 연결 생성 전에 거부한다.
상대경로·..·기존 symlink/junction 및 DB hardlink 별칭도 검사한다. 검사 후 외부에서 경로 링크를
교체하는 경쟁까지 방어하는 파일시스템 보안 기능은 아니다. 운영 파일을 삭제·이동·정리하지 않는다.

```powershell
.\.venv\Scripts\python -m src.ingest.pipeline
.\.venv\Scripts\python -m src.ingest.pipeline --full
.\.venv\Scripts\python -m src.ingest.pipeline --sample-pages 1 --per-page 2
# 네트워크 없이 실행. 운영 DB/raw에는 기록하지 않는다.
.\.venv\Scripts\python -m src.ingest.pipeline --offline-fixture tests/fixtures/a_synthetic_api.json
# 명시한 격리 경로를 재사용하면 fixture pending/replay도 같은 DB에 보존된다.
.\.venv\Scripts\python -m src.ingest.pipeline --offline-fixture tests/fixtures/a_synthetic_api.json --db-path data/a_validation/demo/services.db --raw-dir data/a_validation/demo/raw
# 승인된 라이브 소량 검증. 항상 제한된 표본이며 소실 판정 없음.
.\.venv\Scripts\python -m src.ingest.pipeline --validation --sample-pages 1 --per-page 3
.\.venv\Scripts\python -m pytest tests/ingest -q -ra
.\.venv\Scripts\python -m pytest -q -ra
```

기본 및 --full CLI, 임시 DB 격리 예시는 A 테스트에 있다. `complete/sample` CLI 종료 코드는 0,
`partial/failed`는 1, 잘못된 인수는 2다. 원본 복사 저장 실패가 DB 커밋 뒤 발생하면 DB 요약을 우선한다.
테스트는 키·임베딩·LLM 없이 동작한다. A 로컬 훅은 NotImplementedError를 실패로 유지하며 공용 skip 훅은 건드리지 않는다.
Windows 샌드박스의 pytest 임시 폴더 ACL 제한이 있으면 승인된 일반 터미널에서 실행하거나 접근 가능한 임시 경로를 쓴다.

프로그램 방식도 `pipeline.run_fixture(path, db_path=..., raw_dir=...)`로 같은 격리 규칙을 적용하며
`(list[Chunk], summary)`를 반환한다. 테스트 주입용 `_execute(fetch_page=...)`도 기본 live 저장소를 쓰지 않는다.
기존 `pipeline.run(full=False) -> list[Chunk]`는 일반 live 기본값을 유지한다.
각 store 함수의 선택적 keyword-only `storage=StoragePaths(...)`로 해당 DB의 pending·감사 이력을 조회한다.
기존 인자 없이 호출하면 기존 운영 저장소를 사용한다. 실행 요약에 mode·db_path·raw_dir를 표시한다.
Pytest는 테스트별 tmp_path로 fixture 상태와 legacy 기본 경로를 분리하며 공용 설정·훅은 수정하지 않는다.

## 2차 라이브 smoke 기록 (2026-10-19)

- 실행 ID: `20261019T065611Z-579535bf2e7c`, mode=`validation`, status=`sample`.
- DB: `data/a_validation/live-smoke-pass2-20261019-01/services.db`.
- 원본: `data/a_validation/live-smoke-pass2-20261019-01/raw/<run_id>/`.
- serviceList/serviceDetail/supportConditions 각각 page=1, perPage=3, currentCount=3.
  totalCount와 matchCount는 각 10969였지만 전체 10969건을 수집하거나 검증한 것은 아니다.
- 세 ID `105100000001`, `116010000001`, `119200000001`이 각각 1:1로 결합됐다.
  청년 키워드 기준 포함 1, 제외 2, 조각 3, 미확인 replace 1이며 확인 응답은 하지 않았다.
- 목록·상세의 신청방법 충돌 3건이 기록됐다. 선정기준 등의 실제 빈 문자열도 관측했다.
  기한은 연도 없는 반복 일정·규정/모집공고 참조여서 원문 보존, ISO deadline은 생성하지 않았다.
- 표본에는 HTML 태그가 없었다. HTML 표 수정은 합성 테스트로만 검증했다.
- HTTP/JSON 성공을 실제 관측했으며 인증 실패·rate limit·중간 페이지 실패는 오프라인 mock 검증이다.
  다음 페이지·전체 종료·전체 ID 카디널리티·필터 인자의 동작은 실측하지 않았다.
- 모든 raw·DB·검사 산출물은 Git에서 제외한다. 시작 시 기본 `data/services.db`가 없었고 생성하지 않았다.
  기존 운영 raw는 파일 지문으로 변경 여부를 검사한다. 과거 fixture DB 오염 증거는 없으며 정리하지 않았다.

일일 실행은 Windows 작업 스케줄러에서 매일 트리거, 프로그램에 현재 workspace의
`.venv\Scripts\python.exe`, 인수 `-m src.ingest.pipeline`, 시작 위치에 workspace 절대 경로를 지정한다.
키는 .env/환경에 유지하고 명령 인수에 넣지 않는다. 작업 등록·활성화는 이번 작업에서 하지 않았다.

## 남은 검증

OAS의 JA 기본 라벨 이후 신청자/부양자·경계·특수값·대안 조건 의미를 확인하는 실제 연령 어댑터가 다음 작업이다.
Explicit scoped residence text is supported as documented; structured residence codes and full source coverage remain unverified. Agency inference remains a B retrieval heuristic.
전체 페이지 종료·빈 전체 목록의 권위성·ID 카디널리티는 추가 검증 전까지 자동 소실 권한으로 쓰지 않는다.
Full youth counts, production throughput, actual-model/production-data integration and search/answer quality remain unverified.
기존 B/C fixtures와 eval 질문은 교체하지 않았으며 실제 수집 후 팀원이 정답·공식 근거를 확인해야 한다.

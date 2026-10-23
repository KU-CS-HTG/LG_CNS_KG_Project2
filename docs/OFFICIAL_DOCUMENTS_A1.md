# Official decision documents — A-1 catalog and B/C proposal

Product direction: **Explain detailed criteria and exceptions from the applicable official
documents, with document identity and reproducible source locations.**

A-1 implements registration, bounded original acquisition, identities, temporal inspection,
audit and explicit service links. It does **not** extract passages/clauses/tables, perform OCR,
index documents, retrieve documents, generate answers or calculate entitlement/payments.
Document locations are absent/not assessed until A-2. An official PDF may be accessible to
web-search AI; length alone does not establish a need for RAG or a comparative advantage.

## Existing production contracts stay separate

API ingestion and household inclusion, metadata/deadlines, replay cursors, service pending,
exact acknowledgment, locks, batch verification, associations, B ranking/filters/field boost
and C evidence expansion/citations are unchanged. No legacy Chunk/Citation/validator was widened.
B `build_index()` replaces **all chunks of each supplied service** and deletes stale IDs.
Submitting only document-derived chunks under an existing service_id could erase API evidence.
A-1 therefore never calls that API or writes any document collection/service event.

## Paths and commands

Run in the existing checkout using the established virtual environment. All three paths are
explicit. Document destinations reject resolved aliases/sidecars overlapping configured API
DB/raw/vector storage, reject a preexisting noncatalog DB, and must be separate from each other.
No process-global config mutation selects storage. Runtime `data/official_documents/` and
`data/a_validation/` are ignored; originals/catalogs/attempt diagnostics/locks are not committed.

```powershell
$py = ".\.venv\Scripts\python.exe"
$root = "data/official_documents/a1-housing-20261023"
$catalogArgs = @("--catalog-db", "$root/catalog.db", "--original-dir", "$root/originals", "--diagnostics-dir", "$root/diagnostics")

# Idempotent registration only; no network, API DB or index initialization.
& $py -m src.ingest.official_documents @catalogArgs register

# Explicit primary selection, <=5 sources/command. No arbitrary link crawling.
& $py -m src.ingest.official_documents @catalogArgs collect --source-ids housing-guide-2026 common-guide-2026 housing-selection-2026 --max-mib 32 --attempts 2
# Verified official redistribution of the same national notice; failed primary attempt stays.
& $py -m src.ingest.official_documents @catalogArgs collect --source-ids housing-selection-2026-incheon --max-mib 32 --attempts 2

& $py -m src.ingest.official_documents @catalogArgs inspect --as-of 2026-10-23
& $py -m src.ingest.official_documents @catalogArgs links
& $py -m src.ingest.official_documents @catalogArgs services --service-db data/services.db --query "주거급여"
& $py -m src.ingest.official_documents @catalogArgs services --service-db data/a_validation/region-check-96bff39aff3f4a2c8e60f5f0dc82a807/services.db --query "주거급여"

# Default verification is offline. No GOV24/OpenAI/model/OCR dependencies at runtime.
& $py -m pytest tests/ingest/test_official_documents.py -q -ra
& $py -m pytest tests/ingest -q -ra
& $py -m pytest -q -ra
```

`collect` exits nonzero if any selected source fails, but commits successful acquisitions and
all failure attempts. It continues the bounded selection. A repeated command reacquires bytes
and records another attempt/observation; it does not assume ETag or Last-Modified proves identity.
Inspection/link inspection uses SQLite read-only snapshots and never initializes a missing DB.

### Blocked sources and local originals

The MOLIT LCMS notice endpoint returned a self redirect in the bounded smoke check. That is an
acquisition failure, not repeal, supersession or proof that every official PDF is inaccessible.
Only actual verified posting links may be registered as alternative sources. The Incheon source
is explicitly an **official redistribution** of the national MOLIT notice, not an Incheon-only rule.

```powershell
# A locally supplied original is explicitly UNVERIFIED local content, even when the posting is verified.
& $py -m src.ingest.official_documents @catalogArgs import-local --source-id housing-selection-2026 --file "C:\path\to\supplied-original.pdf"
```

There is no automatic promotion of local content to officially verified evidence. A later
official HTTP observation of identical bytes may corroborate the same artifact/version;
the earlier local observation remains in history. No synthetic/live amendments are invented.

## Identities, storage and audit

`document_catalog.py`, `document_acquire.py`, `official_documents.py` are A-owned. The small
committed `official_sources.json` records checked source facts and evidence; it is not a crawler.
The isolated catalog has these tables (the exact DDL is `document_catalog.DDL`):

| Table | Identity and stored state |
|---|---|
| catalog_meta | Format 1 and resolved originals/diagnostics association |
| documents | Stable SHA-256-derived document_id from explicit publisher/legal logical identity |
| sources | source_id, document_id, independently declared publication_identity, posting/attachment/evidence/facts JSON |
| artifacts | Exact original-byte SHA-256 PK, immutable relative path, byte size, detected format and integrity assessment |
| versions | Stable version_id from document_id + publication_identity + artifact hash; facts snapshot |
| attempts | Unique attempt token, source, UTC start/end, running/success/failure, original source snapshot/failure category |
| observations | Attempt → version, final URL/redirects, collection time, actual media/header hints, origin/assurance |
| relations | Document/optional version endpoints, kind, scope and supporting evidence |
| service_links | Many-to-many document/optional version ↔ actual service ID or explicit unresolved link; topic/evidence/source snapshot |
| catalog_audit | Append-only before/after snapshots, UTC time, reason for registration/metadata/link corrections |

Registration has **no artifact/version yet** until original bytes are successfully obtained.
Year/title/filename/URL alone is never a version identity. Identical bytes may back unrelated
legal identities without merging their versions. Same publication/bytes observed at multiple
verified sources reuses one version and adds observations. Changed bytes at a URL create a
new version; previous originals, facts and observations remain. This does not decide whether
the change is legally an amendment. Exact-byte SHA-256 is not a legal authenticity signature.

Metadata corrections use `correct-metadata --version-id ID --facts facts.json --reason TEXT`;
facts are validated and before/after snapshots audited. Acquisition source snapshots remain
immutable. Re-registering corrected source facts is also audited and does not silently rewrite
earlier version facts. AI source checks always leave human review pending. A human-completed
evidence status requires an explicit human reviewer/type/time receipt; none was generated here.

Writers take OS locks in originals→catalog order. Acquisition temp files are unique per attempt;
validated promotion and artifact/version/observation completion happen under those locks.
SQLite transactions record complete results. Process termination releases lock handles;
file existence alone is not a lease. Interrupted `.part` files are never valid originals.
A file promoted immediately before DB rollback/crash can be orphaned; safe repeat verifies its
hash and registers it. No automatic deletion/reset is used. If auxiliary JSON output fails
after DB commit, the DB attempt/observation remains authoritative and repeat deduplicates.
Direct external catalog/file edits do not participate in this supported protocol.

```python
from pathlib import Path
from src.ingest.document_catalog import Catalog, CatalogPaths
root = Path("data/official_documents/a1-housing-20261023")
catalog = Catalog(CatalogPaths(root / "catalog.db", root / "originals", root / "diagnostics"))
snapshot = catalog.inspect("2026-10-23")  # read-only; no network/model/index/ack
```

## Original integrity and bounded requests

Limits: explicit 1–5 unique registered source IDs/command, at most 64MiB/file, 3 attempts,
3 redirects, 90-second streaming budget/attempt plus bounded connect/read timeouts (10/20s).
The actual smoke uses 32MiB and 2 attempts. Only hosts explicitly supported by the posting
relationship are accepted, including redirects; a government-domain suffix alone is insufficient.
TLS verification remains enabled. Transient HTTP 408/429/selected 5xx and transport failures
retry with bounded backoff; integer Retry-After is capped at 5s. Other failures stop that source.

Binary streaming uses exclusive temporary writes, fsync, exact Content-Length checks when
provided, SHA-256, validated atomic promotion, and content-addressed filenames. HTML/login/error
bodies cannot become successful PDFs. pypdf strict structural reading + EOF/page count checks
record the actual PDF observation (not semantic correctness). Page text/OCR/printed labels/
clauses/tables remain **not assessed**. OLE/HWP-family and ZIP/HWPX/DOCX containers can be
retained with their detected format and limited-integrity status, never renamed into an official
PDF. Unknown/error bodies fail. A failed refresh never deletes an earlier valid original.

## Checked official source set and temporal limits

Source metadata checked on 2026-10-23 (AI official-source check; human review pending):

| source_id | Posting and facts |
|---|---|
| housing-guide-2026 | [MOLIT policyTarget 1065](https://www.molit.go.kr/USR/policyTarget/dtl.jsp?idx=1065), posted 2026-05-08; whole 2026 guide; issuance/effective dates unresolved |
| common-guide-2026 | [MOHW board0009/1489764](https://www.mohw.go.kr/board.es?act=view&bid=0009&list_no=1489764&mid=a10402000000), posted 2026-03-24; whole corresponding guide, household/income topic dependency only; effective dates unresolved |
| housing-selection-2026 | [MOLIT 18653](https://www.molit.go.kr/USR/I0204/m_45/dtl.jsp?idx=18653), posted 2025-09-22; notice 2025-506, issued 2025-09-15, starts 2026-01-01 confirmed by [official rule header](https://www.law.go.kr/conAdmrulByLsPop.do?lsiSeq=201184) |
| housing-selection-2026-incheon | [Incheon 3036478](https://www.incheon.go.kr/housing/hou050300/3036478), redistributed 2025-09-23; same national notice identity and law-header facts, separate source observation |
| housing-implementation-amendment-2026 | [MOLIT 18912](https://www.molit.go.kr/USR/I0204/m_45/dtl.jsp?idx=18912); posted/issued 2026-07-16; effective 2026-07-16 in official rule header; selected partial-amendment attachment, not a whole-guide replacement; original acquisition deferred |
| housing-selection-2027 | [MOLIT 18946](https://www.molit.go.kr/USR/I0204/m_45/dtl.jsp?idx=18946); posted 2026-08-31, notice 2026-455, 2027-01-01 start in posting addendum; finality unresolved because body says 제정고시안; original acquisition deferred |

Exact verified attachment URLs/filenames, field-specific evidence quotes/locations and posting
snapshot hashes are in the source registry. Only selected attachment links are downloaded.
The housing posting explicitly refers household composition and recognized-income calculation
to the MOHW guide. This logical `refers-to` relation preserves its topic scope/evidence without
pretending any page range has been extracted. The July implementation amendment postdates the
May guide: detailed impact review is unresolved, not an automatic entire-guide supersession.
Search was restricted to these postings, their attachment lists and the official rule list;
it does not establish exhaustive legal currency.

### Bounded original acquisition actually executed (2026-10-23)

Isolated root: `data/official_documents/a1-housing-20261023`. Six source registrations describe
four logical documents. The three initial sources were attempted, followed by the verified
Incheon redistribution and one repeated housing-guide download: **5 attempts, 4 successful
observations, 1 failed primary redirect, 3 artifacts and 3 versions**. Each original was capped
at 32MiB/2 attempts; no GOV24/model/index calls occurred. The implementation amendment and
2027 source remain registration-only, without fabricated version/artifact IDs.

| Original | document_id / version_id | Bytes / physical page count |
|---|---|---|
| Housing guide | `doc-de4cd021907ec495ca20900bb30b79b0` / `ver-c8cff856956f9489ac3f278654334ad0` | 13,096,137 / 369 |
| Common guide | `doc-ed78a706b5538ba11823bc1a9114d774` / `ver-9eee4fad75ad76249c6e8db4952c8971` | 10,178,771 / 545 |
| 2026 notice, official Incheon redistribution | `doc-ef7eb50437d2c5e9a735138754d31522` / `ver-6edaead28147090dd3ecb5903ab8ca19` | 88,168 / 1 |

Exact SHA-256 over downloaded original bytes:

```text
housing-guide-2026: 255bfaec8b84c7bb5ee3a6d008ca10e018f6f9752a0e9e4d9d3f0a17b78a8dba
common-guide-2026: 2c9a698db1dbf1deac1e5c7dbe82ad5f726ae90d9f5e70ba2c59c9a88fcf99dd
housing-selection-2026-incheon: 553310b4097a862b99d64717351d8803aa4cec5a30af212ba6b209bdd3e2e065
```

pypdf accepted all three with strict structural reading. It emitted non-zero-indexed xref
warnings on the guides; this is not an archival-conformance or semantic-extraction certification.
Only physical page counts were inspected; no printed page labels/clauses/table locations/text
were extracted or asserted. The repeated housing guide reused its artifact/version, retaining
both observations. The failed MOLIT attachment has no acquired version from that source.

Publication state (final/proposal/unknown) is distinct from dates. As-of inspection uses precise
verified day intervals **[start,end], inclusive**, not newest posting or year-title assumptions.
Proposals are not authoritative. Unknown start remains unresolved; unknown end does not prove
eternal validity. A 2027 source cannot replace a 2026 basis solely because it was posted later.
Competing applicable versions and related/scoped amendments are exposed as review warnings;
substantive legal conflicts are never automatically resolved. Application deadlines are not
document effective-end dates. Uncollected sources/local-only artifacts do not become verified
rules merely because their posting dates/facts are known.

## Real service link boundary

Default `data/services.db` is absent. A read-only inspection of existing validation DBs found
`336000000096`, **주거급여 (맞춤형 급여)**, **국토교통부**, official service URL
`https://www.gov.kr/portal/rcvfvrSvc/dtlEx/336000000096`, version 2 in
`data/a_validation/region-check-96bff39aff3f4a2c8e60f5f0dc82a807/services.db`.
The record's household income threshold and rental/repair support identify the national program.
Similar municipal rent/holiday/veterans programs were inspected but not automatically linked.
The validation record is not asserted to be the current production DB.

The executed local CLI created four links for this actual ID: housing guide/general criteria,
2026 notice/selection and minimum guarantee, common guide/household composition, and common
guide/recognized-income calculation. All record registry identity matched/topic review pending.
The inspected service DB (including its 192 pending records) and selected existing Chroma DB/
index marker/association file were hash-checked unchanged across A-1 acquisition/linking.

Explicit `link` requires document/version identity, topic and evidence JSON. For an actual ID,
it checks the selected DB's service record and exact expected name/agency, records official URL,
status/version, observation time and source DB snapshot. Identity matching leaves topic/human
review pending; it is not a legal approval or universal common-rule inheritance. Missing IDs,
wrong document/version or similar names cannot silently become confirmed links. When no usable
registry exists, use a link without service_id and an explicit `--unresolved-reason`.

```powershell
# ID values come from registration/acquisition, not labels or guessed example IDs.
# This concrete example uses the acquired common-guide version and dependency evidence.
$documentId = "doc-ed78a706b5538ba11823bc1a9114d774"
$versionId = "ver-9eee4fad75ad76249c6e8db4952c8971"
$registry = Get-Content src/ingest/official_sources.json -Raw -Encoding UTF8 | ConvertFrom-Json
$registry.relations[0].evidence | ConvertTo-Json -Depth 10 | Set-Content "$root/link-evidence.json" -Encoding UTF8
& $py -m src.ingest.official_documents @catalogArgs link --document-id $documentId --version-id $versionId --topic "보장가구 구성" --evidence "$root/link-evidence.json" --service-db data/a_validation/region-check-96bff39aff3f4a2c8e60f5f0dc82a807/services.db --service-id 336000000096 --expected-name "주거급여 (맞춤형 급여)" --expected-agency "국토교통부"
```

## Proposed A-2/A-3 contract — team agreement still required

This is a concrete **proposal**, not an implemented interface or evidence of B/C agreement.
Prefer a separate document collection; do not create it during A-1.

| Boundary | Proposed contract |
|---|---|
| API evidence | Existing service_id/Chunk; complete-service replacement/removal and current queue |
| Document identity | document_id, version_id, exact artifact SHA-256; source type/title/publisher/official posting+attachment URLs |
| Document evidence | Separate DocumentEvidence type; stable extraction ID/version, publication/effectivity facts, review/assurance, scoped many-to-many service links |
| Locations | physical PDF page **index 0-based**; printed page label verbatim string or null, independent of physical index; clause path, paragraph span, table/row/cell/footnote IDs only after validated extraction |
| Context | Parent clause, exception, table header/unit and reference relationships; bounded expansion after document retrieval |
| Replacement | Complete **version-scoped** evidence snapshot; a future version must not delete a currently applicable version under the same logical document |
| Document events | Separate durable document queue/consumer; document_id+version_id+event_id+extraction_revision token; exact acknowledgment only after the selected document index verification |
| Index association | Own document-source/index/model association. One globally acknowledged service queue cannot maintain independent API/document collections |
| Citation | Separate DocumentCitation: document/version/artifact identity, publisher/title, exact quote, official URL and reproducible location; optional unresolved printed label must not be invented |

B/C decisions: collection/routing/query-time applicability selection, typed result union, related
clause expansion limits, handling of uncertain dates/scoped amendments, citation UI, extraction
revision/supersession and document consumer verification/ack semantics. Existing API lookup and
C expansion continue unchanged until these decisions are agreed and A-2/A-3 tests pass.

## Future acceptance and evaluation plan (not executed)

A-2 must validate pages/labels, clauses, tables with headers/units, footnotes, exceptions and
cross-references against the originals, with a separate detailed-condition evaluation set.
Specify gold **before looking at retrieval output**: document/version/artifact, required clauses,
mandatory exceptions and physical pages/printed labels; human review must be explicit.

Cover household composition; income inclusion/exclusion; required exceptions; region/household-
size/unit-sensitive tables; current versus future rules; cross-program contamination; and
unsupported questions requiring deferral. No general natural-language entitlement calculator
is introduced by this plan. Existing `eval/questions.jsonl` stays byte-for-byte unchanged.

A-3 acceptance requires isolated document retrieval/indexing/events, exact version replacement,
failure replay/ack, bounded related-clause expansion and reproducible document citations while
retaining API evidence. Future experiments compare API-only versus API+documents with the same
model/questions; web-search AI is a separate date-controlled comparison. Neither experiment,
retrieval/answer quality nor measured superiority has been established in A-1.

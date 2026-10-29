# Category-aware benefit guidance (W5)

Young users may choose one **program-specific** household category. Choosing a
category does not confirm income, assets, dependants, eligibility or payment.
The ordinary source/document/application workflow is retained.

## Public flow

Run `.\.venv\Scripts\python.exe -X utf8 -m app.main` using the installed data pack.
For 근로·자녀장려금 (`105100000001`, 국세청), choose 단독 가구, 홑벌이 가구 or
맞벌이 가구. The default is **아직 모름 / 미선택**. Check the relevant conditions,
enter any supported calculation inputs, press **체크 완료**, then
**내 조건으로 받을 수 있는 지원 보기**. Unanswered conditions remain explicit;
they do not suppress applicable guidance or establish ineligibility.

Only selected-category and common requirements count towards progress. Changing
the category, a checkbox, an input, the source or configuration invalidates the
confirmation/result. Common answers may survive a category change when their
meaning/configuration is unchanged. Other-category inputs and hidden checks cannot
satisfy the new category. Cards have independent namespaces.

**원문 확인하기** deliberately retains complete non-personalized source material.
It is distinct from the filtered result. There is no new retrieval, LLM, embedding
or web request when changing selections or evaluating the result.

## Small optional configuration

`answer_override` accepts an additive `applicability` object; legacy JSONL events
are not migrated or rewritten. `Recommendation`, `Chunk` and citations are unchanged.

- `schema_version`: `applicability-v1`; `program_id`: the actual service ID.
- `groups`: one exclusive group with stable `id`, `label` and options (`id`, `label`).
- `requirements`: stable `id`, source-grounded `text`, applicability and evidence.
- `items`: stable `id`, `kind` (`support`, `amount`, `note`, `calculation`),
  label/text, explicit `requires` **requirement IDs**, applicability and evidence.
- Applicability is `{"kind":"common"}`, or
  `{"kind":"options","group_id":"household","option_ids":["single"]}`,
  or `{"kind":"unresolved"}`. Missing scope becomes unresolved, never common.
- Evidence retains title, URL and source quote. A mapping needs evidence or explicit
  reviewed configuration support; an LLM's asserted scope alone is insufficient.
- A calculation item explicitly references an existing `variant_id`. Legacy generic
  variants without IDs use a deterministic content fingerprint; editing a variant
  requires rechecking that association. No positional references are introduced.

Validation rejects wrong programs, duplicate/unknown IDs, malformed scopes, invalid
requirements and cross-category requirement references. The evaluator separates
common, selected, pending, inapplicable and unresolved items. Inapplicable types
never appear as extra conditions that could unlock another benefit.

Grouped rendering uses this validated configuration as its content authority.
Old generated tiers, mixed summaries, manual support lines, amount guidance and
notes without explicit mappings are withheld; they cannot bypass filtering through
a fallback. Existing non-grouped programs keep their established interface. The
legacy cleaner also flags an invalid-only `requires` list rather than turning it
into unconditional support.

The first read-only source adapter is keyed by the actual program ID, not its name
or a category substring. It recognizes anchored component/household rows in the
stored support/selection text and preserves their exact phrases and source URL.
Unsupported formats are deferred. Its deterministic mapping is **not human approval**.
No formula is added: the current local target has no reviewed calculation rule, so
its source caps are guidance, never estimated personal payments. Household choice
alone does not establish entitlement to either 근로장려금 or 자녀장려금.

## Admin and calculation boundaries

Run `.\.venv\Scripts\python.exe -X utf8 -m app.amount_mode`. In **유형별 적용 설정**,
review stable option/requirement/item IDs, common/named/unresolved scopes, required
conditions and per-item source quotes. Imported legacy entries start unresolved.
Invalid references remain visible and block validation until explicitly corrected.
Only the existing explicit save/confirm action publishes configuration. Opening an
AI draft or preview never approves it. Do not bulk-rewrite historical JSONL records.

The shared evaluator selects only mapped generic-rule variants. Missing/invalid
inputs, adders or unanswered requirements prevent calculation. Unclassified global
rule notes/quotes/conflicts and other variants are not displayed. Unclassified
safety notes/conflicts/open issues block estimates until their applicability is
reviewed; they are never silently discarded to produce a payment. Evidence is shown
from the mapped item. Keep any necessary exception in that item's reviewed text or
as a mapped common note. Existing formulas/amounts are reused without legal updates.

Confirmation fingerprints include card/program identity, full configuration/source
revision, selected options, visible confirmations and calculation inputs. Prepared
cache keys are versioned separately from selection and include source/configuration
content; stale requires-only session data is not treated as the new contract. The
amount-rule reader caches resolved paths and exact bytes, so equal/preserved file
timestamps cannot return another file's rule or keep a stale calculation revision.

## Validation boundary

Focused tests: `python -X utf8 -m pytest tests/test_benefit_applicability.py -q -ra`.
Use the project virtual environment, isolated temp paths and blank API credentials.
Tests use synthetic amounts, mocked external work and Streamlit AppTest. Local-source
AppTest checks are separate from a manual browser check or paid generation. No
ingestion, pending application, model download, reindex or label change is required.

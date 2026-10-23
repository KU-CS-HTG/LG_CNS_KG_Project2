"""검수 이벤트의 형식과 입력 검증. 값 목록(판정·사유·메모 종류 등)은 이 파일 한 곳에서만 정한다."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import date, datetime, timezone

SCHEMA_VERSION = 1

EVENT_TYPES = ("question", "question_status", "run", "judgment", "answer_memo", "service_note", "guideline")

# 판정 4단계와 점수. unsure는 점수 계산에서 제외한다.
LABELS = ("good", "partial", "bad", "unsure")
GRADES = {"good": 2, "partial": 1, "bad": 0}
REASONS = ("not_eligible", "wrong_region", "expired", "irrelevant", "duplicate", "generic_noise", "info_wrong", "other")
UNSURE_REASONS = ("needs_source_check", "needs_discussion", "ambiguous_question")
PROVENANCE = ("human", "claude")

SOURCES = ("team", "synthetic", "paraphrase")        # 실사용 질문은 쓰지 않는다 (사용하는 질문은 모두 동의된 것으로 본다)
SPLITS = ("dev", "test")
DEV_RATIO = 8                                         # 새 질문의 dev:test = 8:2 (질문 ID 해시로 정해 재현 가능)
STATUSES = ("active", "retired", "no_answer", "out_of_scope")
CAND_SOURCES = ("hybrid_filter", "vector", "bm25", "independent_search")
MEMO_KINDS = ("omission", "error", "style", "rule")
MEMO_STATUSES = ("open", "accepted", "rejected", "promoted")
SEVERITIES = ("minor", "major")
NOTE_KINDS = ("deadline_info", "eligibility_nuance", "caution", "contact", "other")
NOTE_STATUSES = ("active", "retired")
GUIDELINE_STATUSES = ("proposed", "active", "retired")
FILTER_KEYS = ("age", "region", "field", "situation", "today")

MAX_NOTE = 500
MAX_TEXT = 1000
MIN_NO_ANSWER_CHECKS = 3
UNKNOWN_VERSION = "t_unknown"
VERSION_FIELDS = ("service_name", "support_target", "selection_criteria", "support_content", "how_to_apply", "application_period")

_REVIEWER = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_SPACE = re.compile(r"\s+")


class ValidationError(ValueError):
    """code는 영문 소문자 식별자, 필요하면 필드 이름이 뒤에 붙는다 (예: invalid_label, missing:reason)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")   # 같은 초의 수정도 순서가 정해지게


def normalize_text(text: str) -> str:
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", str(text or ""))).strip()


def question_id(text: str) -> str:
    """정규화한 문장의 해시. 사람들이 동시에 만들어도 겹치지 않고, 같은 질문이 중복 등록되면 같은 ID가 된다."""
    return "q_" + hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()[:8]


def default_split(qid: str) -> str:
    return "dev" if int(hashlib.sha1(qid.encode("utf-8")).hexdigest()[:8], 16) % 10 < DEV_RATIO else "test"


def service_version(service: dict | None) -> str:
    """검수자가 읽는 6개 항목(서비스명·지원대상·선정기준·지원내용·신청방법·신청기한)의 해시. 메타데이터가 바뀌어도 변하지 않는다."""
    if not service:
        return UNKNOWN_VERSION
    joined = "\x1f".join(normalize_text(service.get(f)) for f in VERSION_FIELDS)
    return "t_" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def new_event_id() -> str:
    return uuid.uuid4().hex


def make_event(type_: str, data: dict, reviewer: str, *, client_event_id: str | None = None, supersedes: str | None = None,
               ts: str | None = None) -> dict:
    event = {"event_id": new_event_id(), "type": type_, "ts": ts or now_iso(), "reviewer": reviewer,
             "schema": SCHEMA_VERSION, "supersedes": supersedes, "data": data}
    if client_event_id:
        event["client_event_id"] = client_event_id
    validate_event(event)
    return event


# --- 검증 ---------------------------------------------------------------------------------------------------
def _fail(code: str, field: str | None = None):
    raise ValidationError(f"{code}:{field}" if field else code)


def _need_str(data: dict, field: str, max_len: int = MAX_TEXT, min_len: int = 1) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not min_len <= len(value.strip()) <= max_len:
        _fail("invalid_text", field)
    return value


def _need_in(data: dict, field: str, allowed) -> str:
    if data.get(field) not in allowed:
        _fail(f"invalid_{field}", field)
    return data[field]


def _need_date(data: dict, field: str) -> date:
    try:
        return date.fromisoformat(str(data.get(field)))
    except ValueError:
        _fail("invalid_date", field)


def validate_event(event: dict) -> None:
    if not isinstance(event, dict):
        _fail("invalid_event")
    if not isinstance(event.get("event_id"), str) or not event["event_id"]:
        _fail("invalid_event_id")
    if event.get("type") not in EVENT_TYPES:
        _fail("invalid_type")
    if event.get("schema") != SCHEMA_VERSION:
        _fail("unsupported_schema")
    if not isinstance(event.get("reviewer"), str) or not _REVIEWER.match(event["reviewer"]):
        _fail("invalid_reviewer")
    try:
        datetime.fromisoformat(str(event.get("ts")).replace("Z", "+00:00"))
    except ValueError:
        _fail("invalid_ts")
    if event.get("supersedes") is not None and not isinstance(event["supersedes"], str):
        _fail("invalid_supersedes")
    if "client_event_id" in event and not isinstance(event["client_event_id"], str):
        _fail("invalid_client_event_id")
    data = event.get("data")
    if not isinstance(data, dict):
        _fail("invalid_data")
    _VALIDATORS[event["type"]](data)


def _v_question(data: dict) -> None:
    text = _need_str(data, "text")
    if "question_id" in data and data["question_id"] != question_id(text):
        _fail("question_id_mismatch", "question_id")
    _need_in(data, "source", SOURCES)
    if data["source"] == "paraphrase" and not data.get("paraphrase_of"):
        _fail("missing", "paraphrase_of")
    if "split" in data and data["split"] not in SPLITS:
        _fail("invalid_split", "split")
    filters = data.get("gold_filters", {})
    if not isinstance(filters, dict) or set(filters) - set(FILTER_KEYS):
        _fail("invalid_gold_filters", "gold_filters")
    if "age" in filters and (type(filters["age"]) is not int or not 0 <= filters["age"] <= 120):
        _fail("invalid_gold_filters", "age")
    tags = data.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        _fail("invalid_tags", "tags")
    for field in ("category", "topic", "expected_answer", "legacy_id"):
        if field in data and data[field] is not None and not isinstance(data[field], str):
            _fail("invalid_text", field)


def _v_question_status(data: dict) -> None:
    if not isinstance(data.get("question_id"), str):
        _fail("missing", "question_id")
    status = _need_in(data, "status", STATUSES)
    if status == "no_answer":
        checks = data.get("checks")
        legacy = data.get("basis") == "legacy_import"
        if not legacy and (not isinstance(checks, list) or len({str(c).strip() for c in checks if str(c).strip()}) < MIN_NO_ANSWER_CHECKS):
            _fail("no_answer_requires_checks", "checks")        # 독립 검색 3회 이상의 기록 (LABELING.md §3)


def _v_run(data: dict) -> None:
    for field in ("run_id", "question_id"):
        if not isinstance(data.get(field), str) or not data[field]:
            _fail("missing", field)
    if not isinstance(data.get("system"), dict):
        _fail("invalid_system", "system")
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        _fail("invalid_candidates", "candidates")
    for c in candidates:
        if (not isinstance(c, dict) or not isinstance(c.get("service_id"), str) or type(c.get("rank")) is not int or c["rank"] < 1
                or c.get("source") not in CAND_SOURCES):
            _fail("invalid_candidates", "candidates")
    answer = data.get("answer")
    if answer is not None and (not isinstance(answer, dict) or not isinstance(answer.get("text"), str)):
        _fail("invalid_answer", "answer")


def _v_judgment(data: dict) -> None:
    for field in ("question_id", "service_id", "service_version"):
        if not isinstance(data.get(field), str) or not data[field]:
            _fail("missing", field)
    label = _need_in(data, "label", LABELS)
    reason, note = data.get("reason"), data.get("note")
    if label == "good":
        if reason is not None:
            _fail("good_must_not_have_reason", "reason")
    elif label == "unsure":
        if reason not in UNSURE_REASONS:
            _fail("invalid_reason", "reason")
    else:                                                              # bad / partial
        if reason not in REASONS:
            _fail("invalid_reason", "reason")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
        _fail("invalid_note", "note")
    if reason == "other" and not (isinstance(note, str) and note.strip()):
        _fail("missing", "note")
    prov = data.get("provenance", {"by": "human"})
    if not isinstance(prov, dict) or prov.get("by") not in PROVENANCE:
        _fail("invalid_provenance", "provenance")


def _v_memo(data: dict) -> None:
    if not isinstance(data.get("run_id"), str) or not data["run_id"]:
        _fail("missing", "run_id")
    _need_in(data, "kind", MEMO_KINDS)
    _need_str(data, "text")
    if data.get("severity", "minor") not in SEVERITIES:
        _fail("invalid_severity", "severity")
    status = data.get("status", "open")
    if status not in MEMO_STATUSES:
        _fail("invalid_status", "status")
    if status == "promoted" and not data.get("guideline_id"):
        _fail("missing", "guideline_id")
    if not isinstance(data.get("scope", {}), dict):
        _fail("invalid_scope", "scope")


def _v_note(data: dict) -> None:
    for field in ("service_id", "service_version"):
        if not isinstance(data.get(field), str) or not data[field]:
            _fail("missing", field)
    _need_in(data, "kind", NOTE_KINDS)
    _need_str(data, "text")
    url = data.get("source_url")
    if not isinstance(url, str) or not re.match(r"^https?://", url):
        _fail("missing", "source_url")                                 # 출처 없는 주석은 쓰지 않는다
    verified, expires = _need_date(data, "verified_at"), _need_date(data, "expires_at")
    if expires < verified:
        _fail("expires_before_verified", "expires_at")
    _need_in(data, "status", NOTE_STATUSES)


def _v_guideline(data: dict) -> None:
    if not isinstance(data.get("guideline_id"), str) or not data["guideline_id"]:
        _fail("missing", "guideline_id")
    _need_str(data, "text")
    status = _need_in(data, "status", GUIDELINE_STATUSES)
    if status == "active" and not data.get("approved_by"):
        _fail("missing", "approved_by")                                # 사람이 승인한 지침만 프롬프트에 쓴다
    for field in ("applies_to", "from_memos"):
        if field in data and not (isinstance(data[field], list) and all(isinstance(x, str) for x in data[field])):
            _fail("invalid_list", field)


_VALIDATORS = {"question": _v_question, "question_status": _v_question_status, "run": _v_run, "judgment": _v_judgment,
               "answer_memo": _v_memo, "service_note": _v_note, "guideline": _v_guideline}

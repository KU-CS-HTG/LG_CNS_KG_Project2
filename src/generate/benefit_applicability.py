"""Program-scoped applicability, independent of eligibility and LLM assertions.

Missing scope is unresolved, never common. Only a validated source adapter or an
explicitly saved configuration supplies mappings. Legacy text remains available
in the source view; it cannot bypass this evaluator in grouped results.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import hashlib
import json
import re
import sqlite3

VERSION = "applicability-v1"
PROGRAM = "105100000001"
KINDS = ("support", "amount", "note", "calculation")
OPTIONS = (("single", "단독 가구"), ("single_earner", "홑벌이 가구"), ("dual_earner", "맞벌이 가구"))
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def scope(options=None) -> dict:
    return {"kind": "common"} if options is None else {"kind": "options", "group_id": "household", "option_ids": list(options)}


def _scope_valid(value, groups) -> bool:
    if not isinstance(value, dict):
        return False
    if value == {"kind": "common"} or value == {"kind": "unresolved"}:
        return True
    ids = value.get("option_ids")
    return (set(value) == {"kind", "group_id", "option_ids"} and value.get("kind") == "options"
            and isinstance(value.get("group_id"), str) and value["group_id"] in groups and isinstance(ids, list) and bool(ids)
            and all(isinstance(i, str) and i in groups[value["group_id"]] for i in ids) and len(set(ids)) == len(ids))


def validate(raw: dict, program_id: str | None = None) -> tuple[dict | None, list[str]]:
    """Validate IDs/references/scopes. Preserve unknown scope without promotion.

    Evidence is a source assertion, not human approval. Publishing still belongs
    to answer_override.save/confirm; this function never writes approval records.
    One exclusive group is intentionally supported in this first feature.
    """
    if not isinstance(raw, dict):
        return None, ["유형 설정은 객체여야 해요."]
    c = deepcopy(raw)
    errors = []
    if c.get("schema_version") != VERSION or not isinstance(c.get("program_id"), str) or not c["program_id"]:
        errors.append("유형 설정 버전·제도 ID를 확인해 주세요.")
    if program_id is not None and c.get("program_id") != program_id:
        errors.append("다른 제도의 유형 설정이에요.")
    groups = {}
    rows = c.get("groups")
    if not isinstance(rows, list) or len(rows) != 1:
        return None, errors + ["상호 배타적인 선택 그룹 하나가 필요해요."]
    for g in rows:
        if not isinstance(g, dict) or not isinstance(g.get("id"), str) or not _ID.fullmatch(g["id"]) or not g.get("label"):
            return None, errors + ["선택 그룹 ID·이름을 확인해 주세요."]
        opts = g.get("options")
        if not isinstance(opts, list) or not opts or any(not isinstance(o, dict) or not isinstance(o.get("id"), str) or not _ID.fullmatch(o["id"]) or not o.get("label") for o in opts):
            return None, errors + ["선택 유형 ID·이름을 확인해 주세요."]
        groups[g["id"]] = {o["id"] for o in opts}
        if len(groups[g["id"]]) != len(opts):
            errors.append("유형 ID가 중복돼요.")
    requirements = c.get("requirements", [])
    items = c.get("items", [])
    if not isinstance(requirements, list) or not isinstance(items, list):
        return None, errors + ["조건·지원 항목은 목록이어야 해요."]
    ids = set()
    for row in requirements + items:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not _ID.fullmatch(row["id"]) or row["id"] in ids:
            return None, errors + ["조건·지원 ID가 없거나 중복돼요."]
        ids.add(row["id"])
        row.setdefault("applicability", {"kind": "unresolved"})
        if not _scope_valid(row["applicability"], groups):
            errors.append(f"{row['id']}: 적용 유형이 올바르지 않아요.")
        evidence = row.get("evidence")
        if row["applicability"] != {"kind": "unresolved"} and (
            not isinstance(evidence, list) or not evidence or any(not isinstance(e, dict) or not isinstance(e.get("quote"), str) or not e["quote"].strip()
            or not str(e.get("url", "")).startswith(("https://", "http://")) for e in evidence)
        ):
            errors.append(f"{row['id']}: 적용 관계의 출처·근거 구절이 필요해요.")
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            errors.append(f"{row['id']}: 내용이 필요해요.")
    if errors:
        return None, errors
    by_req = {r["id"]: r for r in requirements}
    def allowed(s):
        return next(iter(groups.values())) if s.get("kind") == "common" else set(s.get("option_ids", []))
    for row in items:
        refs = row.get("requires")
        if row.get("kind") not in KINDS or not isinstance(refs, list) or any(not isinstance(i, str) or i not in by_req for i in refs):
            errors.append(f"{row['id']}: 지원 종류·조건 ID 참조를 확인해 주세요.")
            continue
        for ref in refs:
            if not allowed(row["applicability"]) <= allowed(by_req[ref]["applicability"]):
                errors.append(f"{row['id']}: 다른 유형 또는 미확인 조건을 참조해요.")
        if row["kind"] == "calculation" and not row.get("variant_id"):
            errors.append(f"{row['id']}: 계산 유형 ID가 필요해요.")
    return (None, errors) if errors else (c, [])


def evaluate(config: dict, selection: dict, checked=()) -> dict:
    """One evaluator for every grouped output. Unchecked means unanswered."""
    c, errors = validate(config)
    out = {k: [] for k in ("common", "selected", "pending", "inapplicable", "unresolved", "requirements")}
    out.update(errors=errors, selection_required=False)
    if errors:
        return out
    groups = {g["id"]: {o["id"] for o in g["options"]} for g in c["groups"]}
    if not isinstance(selection, dict) or set(selection) - set(groups) or any(
        v is not None and (not isinstance(v, str) or v not in groups[k]) for k, v in selection.items()
    ):
        out["errors"] = ["유형 선택이 없거나 서로 충돌해요."]
        return out
    if any(selection.get(g) is None for g in groups):
        out["selection_required"] = True
        return out
    def applies(row):
        a = row["applicability"]
        return a["kind"] == "common" or (a["kind"] == "options" and selection[a["group_id"]] in a["option_ids"])
    out["requirements"] = [r for r in c.get("requirements", []) if applies(r)]
    visible = {r["id"] for r in out["requirements"]}
    if not set(checked) <= visible:
        out["errors"] = ["현재 유형에 속하지 않는 확인 조건이에요."]
        return out
    for item in c.get("items", []):
        if item["applicability"]["kind"] == "unresolved":
            out["unresolved"].append(item)
        elif not applies(item):
            out["inapplicable"].append(item)
        else:
            missing = sorted(set(item["requires"]) - set(checked))
            bucket = "pending" if missing else "common" if item["applicability"]["kind"] == "common" else "selected"
            out[bucket].append({**item, "missing": missing})
    return out


def confirmation(config, key, selection, checked, values) -> str:
    return fingerprint([VERSION, key, config, selection, sorted(checked), values])


def variant_id(variant: dict) -> str:
    return variant.get("id") or "variant-" + fingerprint(variant)[:20]


def calculation(rule, config, selection, checked, values):
    """Select reviewed branches before calling the existing calculator.

    Do not expose unclassified rule-wide notes/quotes or run other variants. Missing
    inputs (including adders) prevent payout, never substitute a maximum.
    """
    from src.generate import amount_rules
    out = evaluate(config, selection, checked)
    if out["errors"] or out["selection_required"] or not rule or rule.get("calculator") != "generic":
        return []
    variants = {variant_id(v): v for v in rule["variants"]}
    if len(variants) != len(rule["variants"]):
        return [{"item": {"label": "계산 연결 검수", "evidence": []}, "pending": ["계산 유형 ID가 중복돼요."]}]
    results = []
    for item in out["common"] + out["selected"] + out["pending"]:
        if item["kind"] != "calculation":
            continue
        variant = variants.get(item["variant_id"])
        if variant is None:
            results.append({"item": item, "pending": ["계산 유형 연결을 다시 검수해 주세요."]})
            continue
        classified_notes = {n["text"] for n in config.get("items", [])
                            if n["kind"] == "note" and n.get("applicability", {}).get("kind") in ("common", "options")}
        if any(note not in classified_notes for note in rule_notes(rule)):
            results.append({"item": item, "pending": ["계산 규칙의 주의사항·예외 적용 범위를 먼저 검수해야 해요."]})
            continue
        needed = calculation_inputs(rule, [variant])
        missing = [s["label"] for s in needed if not valid_input(s, values.get(s["id"]))]
        if item["missing"] or missing:
            results.append({"item": item, "pending": missing, "requirements": item["missing"]})
            continue
        selected_rule = {**rule, "variants": [variant]}
        results.append({"item": item, "output": amount_rules.generic_calculate(selected_rule, values)})
    return results


def rule_notes(rule):
    """Safety qualifiers cannot be silently dropped to obtain a personal estimate."""
    return [s for s in rule.get("open_issues", []) + rule.get("conflicts", []) + rule.get("notes", [])
            + ([rule["general_track_note"]] if rule.get("general_track_note") else []) if isinstance(s, str) and s.strip()]


def valid_input(spec, value):
    """Reject missing/wrong-shaped direct inputs as unconfirmed, not ineligible."""
    import math
    if value is None:
        return False
    if spec["type"] == "bool":
        return isinstance(value, bool)
    if spec["type"] == "choice":
        return value in spec["options"]
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and (spec["type"] != "int" or isinstance(value, int))
            and (spec.get("min") is None or value >= spec["min"]) and (spec.get("max") is None or value <= spec["max"]))


def calculation_inputs(rule, variants):
    needed = set()
    for v in variants:
        for condition in v.get("eligibility", []):
            needed.update(condition[k] for k in ("input", "income_input", "size_input") if k in condition)
        p = v["payout"]
        needed.update(p[k] for k in ("base_input", "by") if k in p)
        needed.update(a["input"] for a in v.get("adders", []))
    return [s for s in rule["inputs"] if s["id"] in needed]


def _read_service(service_id):
    from src.common import config
    if not config.DB_PATH.is_file():
        return None
    with closing(sqlite3.connect(config.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.execute("PRAGMA query_only=ON")
        row = con.execute("SELECT payload FROM services WHERE service_id=? AND status='active'", (service_id,)).fetchone()
        return json.loads(row[0]) if row else None


def config_for(service_id: str, override=None, service=None):
    """Read adapter; no data/history writes, no collection/model initialization."""
    if override and "applicability" in override:
        c, errors = validate(override["applicability"], service_id)
        return c or {"invalid": errors, "program_id": service_id}
    if service_id != PROGRAM:
        return None
    try:
        service = service if service is not None else _read_service(service_id)
    except (OSError, sqlite3.Error, ValueError):
        service = None
    return tax_profile(service or {"service_id": service_id})


def tax_profile(service: dict) -> dict:
    """Exact-ID/anchored-source adapter, not substring classification or approval.

    Relationships follow the source's named household rows under explicit component
    headings. Unknown formats remain unresolved. No amounts/formulas are invented.
    """
    sid = service["service_id"]
    source = service.get("selection_criteria", "")
    support = service.get("support_content", "")
    url = service.get("detail_url", "")
    c = {"schema_version": VERSION, "program_id": sid, "origin": "source_adapter",
         "source_revision": fingerprint([sid, source, support, url]),
         "groups": [{"id": "household", "label": "가구 유형", "options": [{"id": oid, "label": label} for oid, label in OPTIONS]}],
         "requirements": [], "items": []}
    def evidence(text):
        return [{"title": service.get("service_name", "수집 원문"), "url": url, "quote": text}]
    def add(collection, identity, text, a, **extra):
        if text.strip() and url.startswith(("http://", "https://")):
            c[collection].append({"id": identity, "text": text.strip(), "applicability": a, "evidence": evidence(text.strip()), **extra})
    def one(pattern, text):
        found = re.findall(pattern, text, re.M)
        return found[0].strip() if len(found) == 1 else ""
    basis, heading, _ = support.partition("- 근로장려금은")
    if heading and re.fullmatch(r"○ 전년도 연간 부부합산 총 급여액 등\([^()\n]*\)에 따라", basis.strip()):
        add("items", "payout-basis", basis.strip(), scope(), kind="note", label="금액 기준", requires=[])
    names = ("단독", "홑벌이", "맞벌이")
    income, _, rest = source.partition("- 재산요건")
    definitions = rest.partition("- 가구요건")[2]
    property_text = one(r"^ㆍ (전년도 6월 1일 현재[^\n]+)$", rest)
    add("requirements", "property", property_text, scope())
    add("requirements", "income-source", one(r"^○ (아래 요건을 모두 충족하는[^\n]+)$", source), scope())
    exclusions = source.partition("<신청 제외>")[2].partition("※ 총소득")[0].strip()
    add("requirements", "exclusions", exclusions, scope())
    common_refs = [r["id"] for r in c["requirements"]]
    common_complete = set(common_refs) == {"property", "income-source", "exclusions"}
    earned_text, _, child_text = support.partition("- 자녀 장려금은")
    for (oid, _), name in zip(OPTIONS, names):
        a = scope([oid])
        definition = one(r"^ㆍ (" + name + r"\s*가구\s*:[^\n]+)$", definitions)
        if definition:
            next(o for o in c["groups"][0]["options"] if o["id"] == oid).update({"text": definition, "evidence": evidence(definition)})
        inc = one(r"^ㆍ (" + name + r"\s*가구\s*:[^\n]+)$", income)
        add("requirements", "income-" + oid, inc, a)
        refs = common_refs + (["income-" + oid] if inc else [])
        line = one(r"^ㆍ (" + name + r"\s*가구[^\n]+)$", earned_text)
        # Missing common/branch evidence must not turn an item unconditional.
        if heading and common_complete and inc and definition:
            add("items", "earned-" + oid, line, a, kind="support", label="근로장려금", requires=refs)
        child = one(r"^ㆍ (" + name + r"\s*가구[^\n]+)$", child_text)
        if oid == "single":
            add("items", "child-" + oid, child, a, kind="support", label="자녀장려금", requires=[])
        else:
            child_inc = income.partition("(자녀장려금)")[2].strip()
            child_condition = one(r"^2\) (부양자녀[^\n]+)$", definitions)
            if child_inc and child_condition and common_complete:
                add("requirements", "child-income-" + oid, child_inc, a)
                add("requirements", "child-dependent-" + oid, child_condition, a)
                add("items", "child-" + oid, child, a, kind="support", label="자녀장려금",
                    requires=common_refs + ["child-income-" + oid, "child-dependent-" + oid])
    marker = definitions.find("1) 배우자")
    footnotes = definitions[marker:].partition("<신청 제외>")[0].strip() if marker >= 0 else ""
    if footnotes:
        add("items", "definitions", footnotes, scope(), kind="note", label="가구 정의·예외", requires=[])
    comparison = source.partition("※ 총소득과 총급여액 등 비교")[2].partition("* 자세한 신청요건")[0].strip()
    add("items", "income-definition", comparison, scope(), kind="note", label="소득 판정·산정 기준의 구분", requires=[])
    add("items", "calculation-source", one(r"^(\* 자세한 산정식[^\n]+)$", support), scope(), kind="note", label="산정식 확인", requires=[])
    c["warnings"] = ["수집 원문의 유형별 안내이며 사람의 자격 승인이나 개인 수령액 계산이 아니에요."]
    if not any(i["id"].startswith("earned-") for i in c["items"]):
        c["warnings"].append("유형별 원문 연결이 불충분해요. 적용 범위 검수가 필요해요.")
    return c

"""사람이 공식 자료를 보고 예상 수령액 규칙을 직접 넣는 일을 돕는다 (화면: `python -m app.amount_mode`).

수집 원문에 금액이 없어 초안(`amount_draft`)을 만들 수 없는 제도를 찾아 보여 주고, 담당자가 공식 자료에서 읽은 금액·조건을 입력하면
범용 규칙(`amount_rules`의 generic)으로 바꾼다. 입력은 공식 출처(제목·주소·근거 구절)가 꼭 있어야 하고, 결과 규칙은 화면이 저장 전에 미리 보여 준다.
이 모듈은 화면 없이 테스트할 수 있는 계산·검증·저장만 한다."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.common.schema import Citation, Recommendation
from src.generate import amount_candidates, amount_rules

LEVELS = {"공식": "공식(담당자 입력)", "2차": "2차(담당자 입력, 공식 원문 대조 필요)"}
MAX_VARIANTS = 4
SHORT = 140


def needs_official(services: list[dict], demand: dict[str, int] | None = None, *, scope: str = "no_amount",
                   include_local: bool = False, text: str = "") -> list[dict]:
    """사람이 공식 자료를 넣어야 하는 제도 목록. scope='no_amount'는 지원내용에 금액·비율이 없어 초안을 만들 수 없는 제도,
    'all'은 금액이 있는 제도도 포함한다(원문 금액을 공식 값으로 고치고 싶을 때). 규칙 없는 제도·수요 많은 제도·전국 제도가 앞이다."""
    demand, text = demand or {}, text.strip().lower()
    rows = []
    for s in services:
        info = amount_candidates.analyze(s)
        if scope == "no_amount" and info is not None:
            continue
        agency = s.get("agency", "")
        local = bool(amount_candidates._LOCAL_RE.search(agency.strip()))
        if (local and not include_local) or (text and text not in f"{s.get('service_name', '')} {agency} {s.get('service_id', '')}".lower()):
            continue
        sid = s.get("service_id", "")
        rows.append({"service_id": sid, "service_name": s.get("service_name", ""), "agency": agency, "field": s.get("field", ""),
                     "demand": demand.get(sid, 0), "local": local, "has_rule": amount_rules.find(sid) is not None,
                     "reason": "지원내용에 금액·비율이 없음" if info is None else "금액은 있지만 공식 값으로 입력"})
    rows.sort(key=lambda r: (r["has_rule"], -r["demand"], r["local"], r["service_name"], r["service_id"]))
    return rows


def _short(text: str, n: int = SHORT) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= n else one[:n - 1] + "…"


def preview_recommendation(service: dict, needs_check: list[str] | None = None) -> Recommendation:
    """이 제도가 답변에 나올 때의 카드(미리보기용). 판정 이유 등 생성 단계 값은 안내 문구로 대신한다."""
    period = service.get("application_period") or ""
    content = (service.get("support_content") or "").strip()
    return Recommendation(
        service_name=service.get("service_name", ""), agency=service.get("agency", ""),
        reason="(미리보기) 이 제도가 답변으로 나올 때의 카드예요.", benefit=_short(content) or "지원내용 정보 없음",
        how_to_apply=_short(service.get("how_to_apply") or "") + (f" / 신청기한: {period}" if period else ""),
        needs_check=list(needs_check or []),
        citations=[Citation(service.get("service_name", ""), service.get("agency", ""), _short(content, 200), service.get("detail_url") or "")],
        service_id=service.get("service_id", ""), benefit_detail=[content] if content else [])


def preview_tiers(service: dict) -> dict:
    """'내 조건으로 받을 수 있는 지원' 정리 결과의 대용. 미리보기는 LLM을 부르지 않으므로 원문 첫 줄을 한 항목으로 둔다.
    실제 답변에서는 LLM이 지원 유형을 나누므로 항목 이름·개수는 다를 수 있다."""
    content = (service.get("support_content") or "").strip()
    first = next((l.strip() for l in content.splitlines() if l.strip()), "")
    return {"tiers": [{"label": "지원 내용", "requires": [], "benefit": _short(first) or "지원내용 정보 없음", "quote": first[:80] if len(first) >= 4 else "",
                       "basis": "source" if len(first) >= 4 else "outside", "amount": "", "amount_basis": "", "evidence": []}], "notes": []}


def form_to_rule(service: dict, form: dict, editor: str = "", today: date | None = None) -> tuple[dict | None, list[str]]:
    """입력 폼 -> (범용 규칙, 문제 목록). 문제가 있으면 규칙은 None이다.
    form: {variants: [{name, pay_type: fixed|percent, amount_won, months, rate_pct, cap_won, age_min, age_max, median_pct, no_house}],
           source: {title, url, level: 공식|2차, quote}, note}"""
    problems: list[str] = []
    src = form.get("source") or {}
    title, url, quote = (str(src.get(k) or "").strip() for k in ("title", "url", "quote"))
    level = src.get("level") if src.get("level") in LEVELS else None
    if not title:
        problems.append("공식 자료의 제목을 입력해 주세요.")
    if not url.startswith(("https://", "http://")):
        problems.append("공식 자료의 주소(https://…)를 입력해 주세요.")
    if len(quote) < 8:
        problems.append("공식 자료에서 금액·조건이 적힌 구절을 그대로 붙여 넣어 주세요.")
    if level is None:
        problems.append("자료 등급(공식/2차)을 골라 주세요.")
    inputs: dict[str, dict] = {}
    variants = []
    for i, v in enumerate((form.get("variants") or [])[:MAX_VARIANTS], 1):
        name = str(v.get("name") or "").strip()
        tag = f"유형 {i}"
        if not name:
            problems.append(f"{tag}: 이름을 입력해 주세요.")
            continue
        months = int(v.get("months") or 0)
        if not 0 <= months <= 600:
            problems.append(f"{tag}: 지급 개월 수는 0~600이어야 해요 (한 번만 받으면 0).")
            continue
        if v.get("pay_type") == "percent":
            rate = float(v.get("rate_pct") or 0)
            if not 0 < rate <= 100:
                problems.append(f"{tag}: 지원 비율(%)은 0보다 크고 100 이하여야 해요.")
                continue
            payout = {"type": "percent", "rate": rate / 100, "base_input": "cost", **({"cap_won": int(v["cap_won"])} if v.get("cap_won") else {}),
                      **({"months": months} if months else {})}
            inputs["cost"] = {"id": "cost", "label": "월 지출 비용 (만원)", "type": "int", "unit_won": 10000, "min": 0}
        else:
            amount = int(v.get("amount_won") or 0)
            if amount <= 0:
                problems.append(f"{tag}: 금액(원)을 입력해 주세요.")
                continue
            payout = {"type": "fixed", "amount_won": amount, **({"months": months, "per": "월"} if months else {"per": "회"})}
        elig = []
        lo, hi = int(v.get("age_min") or 0), int(v.get("age_max") or 0)
        if lo or hi:
            if (hi and lo > hi) or hi > 120:
                problems.append(f"{tag}: 나이 범위가 올바르지 않아요.")
                continue
            elig.append({"type": "range", "input": "age", "min": lo or None, "max": hi or None,
                         "label": f"만 {lo or 0}~{hi}세" if hi else f"만 {lo}세 이상"})
            inputs["age"] = {"id": "age", "label": "만 나이", "type": "int", "min": 0, "max": 120}
        pct = int(v.get("median_pct") or 0)
        if pct:
            if not 1 <= pct <= 1000:
                problems.append(f"{tag}: 중위소득 비율이 올바르지 않아요.")
                continue
            elig.append({"type": "median_income", "pct": pct, "income_input": "income", "size_input": "size", "label": f"기준 중위소득 {pct}% 이하"})
            inputs["income"] = {"id": "income", "label": "월소득 (만원, 심사 기준 가구)", "type": "int", "unit_won": 10000, "min": 0}
            inputs["size"] = {"id": "size", "label": "가구원 수 (본인 포함)", "type": "int", "min": 1, "max": 10}
        if v.get("no_house"):
            elig.append({"type": "is_true", "input": "no_house", "label": "무주택"})
            inputs["no_house"] = {"id": "no_house", "label": "무주택자인가요?", "type": "bool", "from_check": "무주택"}
        variants.append({"name": name, "quote": quote, "eligibility": elig, "payout": payout})
    if not variants and not any("유형" in p for p in problems):
        problems.append("지원 유형을 하나 이상 입력해 주세요.")
    if problems:
        return None, problems
    note = str(form.get("note") or "").strip()
    status = (f"담당자 입력({editor or '이름 없음'}, {(today or date.today()).isoformat()}): "
              + ("공식 자료 기준" if level == "공식" else "2차 자료 기준, 공식 원문 대조 필요"))
    rule = {"service_ids": [service["service_id"]], "name": service.get("service_name", ""), "calculator": "generic",
            "as_of": (today or date.today()).isoformat(), "status": status,
            "sources": [{"title": title, "url": url, "level": LEVELS[level], "facts": f"“{quote}”"}],
            "conflicts": [], "open_issues": ([note] if note else []) + ["금액은 소득·가구 등에 따라 달라질 수 있어요. 최종 내용은 담당 기관에서 확인해 주세요."],
            "inputs": list(inputs.values()), "variants": variants}
    if pct_used(variants):
        rule["facts"] = {"median_income_monthly_won": amount_rules.MEDIAN_INCOME_2026}
    return rule, check_rule(rule)


def pct_used(variants: list[dict]) -> bool:
    return any(c["type"] == "median_income" for v in variants for c in v["eligibility"])


def check_rule(rule: dict) -> list[str]:
    """범용 규칙의 형식 검사 (직접 고친 JSON도 저장 전에 거른다). 문제 목록, 비면 통과."""
    problems = []
    if not isinstance(rule, dict) or rule.get("calculator") != "generic":
        return ["calculator는 generic이어야 해요."]
    if not (isinstance(rule.get("service_ids"), list) and rule["service_ids"] and all(isinstance(s, str) and s for s in rule["service_ids"])):
        problems.append("service_ids가 필요해요.")
    for key in ("name", "as_of", "status"):
        if not str(rule.get(key) or "").strip():
            problems.append(f"{key}가 필요해요.")
    sources = rule.get("sources")
    if not (isinstance(sources, list) and sources and all(isinstance(s, dict) and all(str(s.get(k) or "").strip() for k in ("title", "url", "level", "facts")) for s in sources)):
        problems.append("출처(sources)마다 title·url·level·facts가 필요해요.")
    ids = {}
    for spec in rule.get("inputs") or []:
        if not (isinstance(spec, dict) and spec.get("id") and spec.get("type") in amount_rules.INPUT_TYPES and spec.get("label")) or spec["id"] in ids:
            problems.append(f"입력 형식이 올바르지 않아요: {str(spec)[:60]}")
        else:
            ids[spec["id"]] = spec
    variants = rule.get("variants")
    if not (isinstance(variants, list) and variants):
        return problems + ["variants가 필요해요."]
    for v in variants:
        name = v.get("name") if isinstance(v, dict) else None
        if not name:
            problems.append("유형 이름이 필요해요.")
            continue
        for c in v.get("eligibility") or []:
            refs = [c.get(k) for k in ("input", "income_input", "size_input") if isinstance(c, dict) and c.get(k) is not None]
            if not isinstance(c, dict) or c.get("type") not in amount_rules.COND_TYPES or not refs or any(r not in ids for r in refs):
                problems.append(f"{name}: 조건이 올바르지 않아요 {str(c)[:50]}")
        p = v.get("payout")
        if not isinstance(p, dict) or p.get("type") not in amount_rules.PAYOUT_TYPES:
            problems.append(f"{name}: 지급 방식이 올바르지 않아요.")
        elif p["type"] == "fixed" and not (isinstance(p.get("amount_won"), (int, float)) and p["amount_won"] > 0):
            problems.append(f"{name}: 금액이 올바르지 않아요.")
        elif p["type"] == "percent" and (p.get("base_input") not in ids or not 0 < (p.get("rate") or 0) <= 1):
            problems.append(f"{name}: 비율 지급 설정이 올바르지 않아요.")
        elif p["type"] == "table" and (p.get("by") not in ids or not p.get("rows")):
            problems.append(f"{name}: 구간표가 올바르지 않아요.")
        for a in v.get("adders") or []:
            if not (isinstance(a, dict) and a.get("input") in ids and a.get("per_won")):
                problems.append(f"{name}: 가산 설정이 올바르지 않아요.")
    return problems


def save_rule(rule: dict, rules_path: Path | None = None) -> str:
    """규칙 파일에 저장한다. 같은 service_id의 범용 규칙은 바꾸고(국민취업지원제도처럼 코드 계산이 필요한 규칙은 거부), 없으면 추가한다.
    저장 뒤 수정 시각이 바뀌므로 실행 중인 앱도 다음 질문부터 새 규칙을 읽는다. 'replaced' 또는 'added'."""
    rules_path = rules_path or amount_rules.RULES_PATH
    problems = check_rule(rule)
    if problems:
        raise ValueError("; ".join(problems))
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    for i, old in enumerate(data["rules"]):
        if set(old["service_ids"]) & set(rule["service_ids"]):
            if old.get("calculator") != "generic":
                raise ValueError(f"코드로 계산하는 규칙({old.get('name')})은 화면에서 바꿀 수 없어요.")
            data["rules"][i] = rule
            outcome = "replaced"
            break
    else:
        data["rules"].append(rule)
        outcome = "added"
    rules_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    amount_rules._load.cache_clear()
    return outcome

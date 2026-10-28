"""수집 원문에서 예상 수령액 규칙 '초안'을 만든다 (후보 발굴 -> 초안 -> 사람 검수 -> 규칙 파일).

    python -m src.generate.amount_draft draft --service-id 161300000099 [--db data/services.db | --raw data/raw/serviceList.json]
    python -m src.generate.amount_draft promote data/a_validation/amount_drafts/161300000099.json

- `draft`: LLM이 지원내용·지원대상·선정기준 원문을 범용 규칙 형식(`amount_rules`의 generic)으로 옮기고, 코드가 검증한다.
  규칙의 모든 숫자(금액·비율·기간·나이·소득 기준)는 원문에 있어야 하고 유형마다 원문 구절(quote)이 글자 그대로 있어야 한다.
  어긋난 유형은 버리고 이유를 남긴다 (조건 숫자를 지어내면 자격이 느슨해지므로 조건을 고치지 않고 유형째 버린다).
  service_ids·이름·출처·상태·확인일은 LLM이 아니라 코드가 채운다. 초안은 규칙 파일에 넣기 전까지 화면에 쓰이지 않는다.
- `promote`: 사람이 초안을 읽고 고친 뒤 `amount_rules.json`에 넣는다 (형식 재검증, service_id 중복 거부).
- 초안의 값은 수집 원문(보조금24) 기준이다. 공식 원문·법령과 대조해 status를 직접 고치기 전까지 '초안'으로 표시된다."""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

from src.common import config
from src.generate import amount_candidates, amount_rules

DRAFT_DIR = config.ROOT / "data" / "a_validation" / "amount_drafts"
STATUS = "초안: 수집 원문(보조금24)에서 자동 정리, 공식 원문 대조 전"
MAX_VARIANTS = 4

PROMPT = """너는 정부 지원제도 원문을 '예상 수령액 계산 규칙'(JSON)으로 옮긴다. JSON 객체 하나만 출력한다.
입력은 제도 이름과 [원문](지원대상·선정기준·지원내용)이다. 원문에 적힌 것만 옮기고, 없는 조건·금액을 만들지 않는다.

출력 형식:
{"inputs": [{"id": 영문 소문자_밑줄, "label": 사용자에게 묻는 한국어 질문, "type": "int"|"float"|"bool"|"choice",
             "unit_won": 만원 단위로 받으면 10000 (금액 입력일 때만), "min": 최소, "max": 최대, "options": choice일 때 선택지, "from_check": 생략},
            ...],
 "variants": [{"name": 지원 유형 이름, "quote": [원문]에서 글자 그대로 복사한 근거 구절,
               "eligibility": [조건...], "payout": {...}, "adders": [{"input": 입력 id, "per_won": 1인당 금액, "max_won": 상한, "label": 이름}]}]}

조건(eligibility) 종류:
 {"type": "range", "input": id, "min": 숫자, "max": 숫자, "label": "만 19~34세"}  - 나이·자녀 수 같은 숫자 범위
 {"type": "is_true"|"is_false", "input": bool 입력 id, "label": "무주택"}
 {"type": "in", "input": choice 입력 id, "values": [...], "label": ...}
 {"type": "median_income", "pct": 60, "income_input": 월소득 입력 id(unit_won 10000), "size_input": 가구원 수 입력 id, "label": "중위소득 60% 이하"}
지급(payout) 종류:
 {"type": "fixed", "amount_won": 원 단위 정수, "months": 지급 개월 수(1회성이면 생략), "per": "월"|"회"|"연"}
 {"type": "percent", "rate": 0.2, "base_input": 비용 입력 id, "cap_won": 한도(원), "months": 개월 수}
 {"type": "table", "by": 입력 id, "rows": [{"min": 숫자, "max": 숫자, "amount_won": 원, "label": "1인 가구"}], "months": 개월 수}

규칙:
- 금액·비율·기간·나이·소득 기준 숫자는 [원문]에 있는 값만 쓴다. 계산하거나 추정하지 않는다.
- 사용자 조건에 따라 금액이 다르면 유형(variant)이나 table로 나눈다. 모든 유형은 [원문] 구절(quote)을 근거로 단다.
- 원문에서 계산할 수 없는 제도(금액이 없음, 현물·서비스)는 variants를 빈 배열로 둔다.
- 유형은 최대 4개. 사용자에게 묻는 입력은 꼭 필요한 것만 (나이, 무주택 여부, 소득, 가구원 수 등)."""


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


_UNIT = {"억": 10**8, "천만": 10**7, "백만": 10**6, "만": 10**4, "천": 10**3}
_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(억|천만|백만|만|천)?")


def source_numbers(text: str) -> set[float]:
    """원문에 나온 숫자와 금액('30만원' -> 300000, '1억 2천만원' -> 120000000). 규칙 숫자를 대조하는 데 쓴다."""
    out: set[float] = set()
    for m in _NUM_RE.finditer(text or ""):
        n = float(m.group(1).replace(",", ""))
        out.add(n)
        if m.group(2):
            out.add(n * _UNIT[m.group(2)])
    for m in re.finditer(r"(\d[\d,]*)\s*억\s*(\d[\d,]*)\s*(천만|백만|만|천)?", text or ""):
        out.add(float(m.group(1).replace(",", "")) * 10**8 + float(m.group(2).replace(",", "")) * _UNIT.get(m.group(3), 1))
    for m in re.finditer(r"(\d+)\s*년", text or ""):
        out.add(float(m.group(1)) * 12)              # 'n년' -> 개월 수
    return out


def _in(numbers: set[float], value, scale: float = 1.0) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and any(abs(n * scale - value) < 1e-6 for n in numbers)


def validate_rule(raw: dict, source: str) -> tuple[dict | None, list[str]]:
    """LLM이 만든 raw 규칙(inputs, variants)을 검증한다 -> (정리된 {inputs, variants} 또는 None, 문제 목록)."""
    issues: list[str] = []
    nums, flat = source_numbers(source), _squash(source)
    inputs = []
    for spec in raw.get("inputs") or []:
        if not (isinstance(spec, dict) and re.fullmatch(r"[a-z][a-z0-9_]*", str(spec.get("id") or "")) and spec.get("type") in amount_rules.INPUT_TYPES
                and str(spec.get("label") or "").strip()) or any(s["id"] == spec["id"] for s in inputs):
            issues.append(f"입력 형식이 올바르지 않아 버렸어요: {str(spec)[:60]}")
            continue
        clean = {k: spec[k] for k in ("id", "label", "type", "unit_won", "min", "max", "options") if k in spec}
        if clean["type"] == "choice" and not (isinstance(clean.get("options"), list) and clean["options"]):
            issues.append(f"선택지가 없어 입력을 버렸어요: {clean['id']}")
            continue
        inputs.append(clean)
    ids = {s["id"]: s for s in inputs}
    variants = []
    for v in (raw.get("variants") or [])[:MAX_VARIANTS]:
        name = str(v.get("name") or "").strip() if isinstance(v, dict) else ""
        problem = _variant_problem(v, name, ids, nums, flat) if name else "이름이 없어요"
        if problem:
            issues.append(f"유형 '{name or '?'}'을 버렸어요: {problem}")
            continue
        variants.append({"name": name, "quote": str(v["quote"]).strip(), "eligibility": v.get("eligibility") or [], "payout": v["payout"],
                         **({"adders": v["adders"]} if v.get("adders") else {})})
    if not variants:
        return None, issues + ["검증을 통과한 유형이 없어요."]
    used = {c.get(k) for v in variants for c in v["eligibility"] for k in ("input", "income_input", "size_input")} | \
           {v["payout"].get(k) for v in variants for k in ("base_input", "by")} | {a["input"] for v in variants for a in v.get("adders", [])}
    return {"inputs": [s for s in inputs if s["id"] in used], "variants": variants}, issues


def _variant_problem(v: dict, name: str, ids: dict, nums: set[float], flat: str) -> str:
    quote = _squash(str(v.get("quote") or ""))
    if len(quote) < 4 or quote not in flat:
        return "근거 구절(quote)이 원문에 글자 그대로 없어요"
    for c in v.get("eligibility") or []:
        if not isinstance(c, dict) or c.get("type") not in amount_rules.COND_TYPES:
            return f"조건 형식 오류 {str(c)[:40]}"
        refs = [c.get(k) for k in ("input", "income_input", "size_input") if c.get(k) is not None]
        if not refs or any(r not in ids for r in refs):
            return f"조건이 없는 입력을 가리켜요 {refs}"
        if c["type"] == "range" and not all(c.get(k) is None or _in(nums, c[k]) for k in ("min", "max")):
            return f"조건 숫자 {c.get('min')}~{c.get('max')}가 원문에 없어요"
        if c["type"] == "median_income" and not _in(nums, c.get("pct")):
            return f"중위소득 비율 {c.get('pct')}가 원문에 없어요"
        if c["type"] == "in" and not isinstance(c.get("values"), list):
            return "in 조건에 values가 없어요"
    p = v.get("payout")
    if not isinstance(p, dict) or p.get("type") not in amount_rules.PAYOUT_TYPES:
        return "지급 형식 오류"
    if p.get("months") is not None and not _in(nums, p["months"]):
        return f"지급 개월 수 {p['months']}가 원문에 없어요"
    if p["type"] == "fixed" and not _in(nums, p.get("amount_won")):
        return f"금액 {p.get('amount_won')}원이 원문에 없어요"
    if p["type"] == "percent":
        if not _in(nums, p.get("rate"), 0.01) and not _in(nums, (p.get("rate") or 0) * 100):
            return f"비율 {p.get('rate')}가 원문에 없어요"
        if p.get("cap_won") is not None and not _in(nums, p["cap_won"]):
            return f"한도 {p['cap_won']}원이 원문에 없어요"
        if p.get("base_input") not in ids:
            return "비용 입력이 없어요"
    if p["type"] == "table":
        if p.get("by") not in ids or not isinstance(p.get("rows"), list) or not p["rows"]:
            return "구간표가 올바르지 않아요"
        if not all(isinstance(r, dict) and _in(nums, r.get("amount_won")) for r in p["rows"]):
            return "구간 금액 중 원문에 없는 값이 있어요"
    for a in v.get("adders") or []:
        if not (isinstance(a, dict) and a.get("input") in ids and _in(nums, a.get("per_won")) and (a.get("max_won") is None or _in(nums, a["max_won"]))):
            return "가산 규칙의 값이 원문에 없거나 형식이 틀려요"
    return ""


def source_text(service: dict) -> str:
    return "\n\n".join(f"[{title}]\n{service.get(key) or ''}" for title, key in (("지원대상", "support_target"), ("선정기준", "selection_criteria"), ("지원내용", "support_content")))


def draft(service: dict, chat_json=None, today: date | None = None) -> tuple[dict | None, list[str]]:
    """제도 하나의 규칙 초안 -> (규칙 dict 또는 None, 문제 목록)."""
    if chat_json is None:
        from src.generate import condition_extractor
        chat_json = condition_extractor._chat_json
    source = source_text(service)
    raw = chat_json(PROMPT, json.dumps({"제도 이름": service.get("service_name", ""), "원문": source}, ensure_ascii=False))
    clean, issues = validate_rule(raw, source)
    if clean is None:
        return None, issues
    rule = {"service_ids": [service["service_id"]], "name": service.get("service_name", ""), "calculator": "generic",
            "as_of": (today or date.today()).isoformat(), "status": STATUS,
            "sources": [{"title": f"{service.get('service_name', '')} (보조금24 수집 원문)", "url": service.get("detail_url") or "https://www.gov.kr",
                         "level": "수집 원문(보조금24 API)", "facts": "지원대상·선정기준·지원내용 원문에서 자동 정리한 금액·조건"}],
            "conflicts": [], "open_issues": ["자동 정리한 초안이에요. 공식 원문(담당 기관 안내)과 대조해 확인해 주세요."] + issues, **clean}
    if any(c["type"] == "median_income" for v in rule["variants"] for c in v["eligibility"]):
        rule["facts"] = {"median_income_monthly_won": amount_rules.MEDIAN_INCOME_2026}
    return rule, issues


def promote(draft_path: Path, rules_path: Path = amount_rules.RULES_PATH) -> str:
    """초안 파일을 규칙 파일에 넣는다. 형식을 다시 검증하고 이미 있는 service_id는 거부한다."""
    rule = json.loads(Path(draft_path).read_text(encoding="utf-8"))
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    problems = []
    if rule.get("calculator") != "generic" or not rule.get("service_ids") or not rule.get("variants") or not rule.get("sources"):
        problems.append("calculator=generic, service_ids, variants, sources가 필요해요")
    clash = [sid for sid in rule.get("service_ids", []) if any(sid in r["service_ids"] for r in data["rules"])]
    if clash:
        problems.append(f"이미 규칙이 있는 service_id: {clash}")
    if problems:
        raise ValueError("; ".join(problems))
    data["rules"].append(rule)
    rules_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    amount_rules._load.cache_clear()
    return rule["name"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="예상 수령액 규칙 초안 만들기·규칙 파일에 넣기")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft", help="제도 하나의 규칙 초안을 만든다")
    d.add_argument("--service-id", required=True)
    d.add_argument("--db", default=str(config.DB_PATH))
    d.add_argument("--raw", help="DB 대신 수집 원본 serviceList.json")
    d.add_argument("--out", default=str(DRAFT_DIR), help="초안 저장 폴더")
    pr = sub.add_parser("promote", help="검토한 초안을 amount_rules.json에 넣는다")
    pr.add_argument("draft_file")
    args = ap.parse_args(argv)
    if args.cmd == "promote":
        print(f"규칙 파일에 추가했어요: {promote(Path(args.draft_file))}")
        return 0
    services = amount_candidates.load_raw(Path(args.raw)) if args.raw else amount_candidates.load_active(Path(args.db))
    service = next((s for s in services if s["service_id"] == args.service_id), None)
    if service is None:
        raise SystemExit(f"service_id {args.service_id}를 찾지 못했어요.")
    rule, issues = draft(service)
    for issue in issues:
        print("-", issue)
    if rule is None:
        raise SystemExit("규칙 초안을 만들지 못했어요 (원문에 계산할 금액·조건이 없거나 검증을 통과하지 못함).")
    out = Path(args.out) / f"{args.service_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rule, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"초안을 저장했어요: {out}\n내용을 읽고 고친 뒤 `python -m src.generate.amount_draft promote {out}` 로 규칙 파일에 넣으세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

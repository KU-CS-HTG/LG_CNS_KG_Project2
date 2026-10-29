"""제도별 '예상 수령액' 계산 (국민취업지원제도부터).

규칙(금액·기간·기준·출처·확인일)은 `amount_rules.json`에 사람이 정해 두고, 사용자가 입력한 값으로 금액을 코드가 계산한다 (LLM 미사용, 같은
입력이면 같은 결과). 시나리오(본인 단독 가구 / 가구 합산)를 나눠 보여 주고, 규칙으로 판단하지 못하는 것은 '남은 변수'로 돌려준다.
계산 값은 참고용이며, 규칙에 없는 제도는 `find`가 None을 돌려줘 카드가 기존 방식 그대로 동작한다."""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

RULES_PATH = Path(__file__).with_name("amount_rules.json")
MAN = 10_000

# 입력칸 정의 (id, 라벨, 종류, 기본값, 범위 또는 선택지). 종류: int | float | choice
INPUTS = {
    "kupj": [
        {"id": "age", "label": "만 나이", "type": "int", "default": 25, "min": 0, "max": 100},
        {"id": "worked", "label": "최근 2년 안에 100일 또는 800시간 이상 일했나요?", "type": "choice", "default": "아니요", "options": ["예", "아니요"]},
        {"id": "joined_before", "label": "이전에 국민취업지원제도에 참여한 적이 있나요?", "type": "choice", "default": "없어요", "options": ["없어요", "있어요"]},
        {"id": "restricted", "label": "실업급여·생계급여를 받고 있거나 받은 지 6개월 이내인가요?", "type": "choice", "default": "아니요", "options": ["아니요", "예"]},
        {"id": "dep_minor", "label": "만 18세 이하 가족 수 (본인 제외)", "type": "int", "default": 0, "min": 0, "max": 10},
        {"id": "dep_senior", "label": "만 70세 이상 가족 수", "type": "int", "default": 0, "min": 0, "max": 10},
        {"id": "dep_disabled", "label": "중증장애인 가족 수", "type": "int", "default": 0, "min": 0, "max": 10},
        {"id": "own_income_man", "label": "[단독 가구로 볼 때] 본인 월소득 (만원)", "type": "int", "default": 0, "min": 0, "max": 100000},
        {"id": "own_asset_eok", "label": "[단독 가구로 볼 때] 본인 재산 합계 (억원, 부채 공제 후)", "type": "float", "default": 0.0, "min": 0.0, "max": 1000.0},
        {"id": "house_size", "label": "[가구 합산으로 볼 때] 가구원 수 (본인 포함)", "type": "int", "default": 1, "min": 1, "max": 10},
        {"id": "house_income_man", "label": "[가구 합산으로 볼 때] 가구 월소득 (만원)", "type": "int", "default": 0, "min": 0, "max": 100000},
        {"id": "house_asset_eok", "label": "[가구 합산으로 볼 때] 가구 재산 합계 (억원, 부채 공제 후)", "type": "float", "default": 0.0, "min": 0.0, "max": 1000.0},
    ],
}


@lru_cache(maxsize=4)
def _read(mtime_ns: int) -> list[dict]:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))["rules"]


def _load() -> list[dict]:
    """규칙 파일 내용. 파일이 바뀌면(수정 시각이 달라지면) 앱을 다시 띄우지 않아도 새 규칙을 읽는다."""
    return _read(RULES_PATH.stat().st_mtime_ns)


_load.cache_clear = _read.cache_clear

_preview = threading.local()          # 규칙 관리 화면의 미리보기 전용 (이 화면을 그리는 스레드에서만 보인다)


@contextmanager
def preview_rule(rule: dict | None):
    """이 블록 안에서는 find()가 저장하지 않은 규칙(rule)을 돌려준다 (규칙 관리 화면의 '입력 후' 미리보기)."""
    previous = getattr(_preview, "rule", None)
    _preview.rule = rule
    try:
        yield
    finally:
        _preview.rule = previous


def find(service_id: str) -> dict | None:
    pending = getattr(_preview, "rule", None)
    if pending is not None and service_id and service_id in pending["service_ids"]:
        return pending
    return next((r for r in _load() if service_id and service_id in r["service_ids"]), None)


def inputs_for(rule: dict) -> list[dict]:
    return rule["inputs"] if rule["calculator"] == "generic" else INPUTS[rule["calculator"]]


def won(n: int) -> str:
    """원 -> '6억 원' / '360만 원' / '11만 6천 원' / '약 308만 원'(천 원 단위로 떨어지지 않는 큰 금액) / '1,234원'."""
    if n % 100_000_000 == 0 and n >= 100_000_000:
        return f"{n // 100_000_000:,}억 원"
    man, rest = divmod(n, MAN)
    if rest == 0:
        return f"{man:,}만 원"
    if rest % 1000 == 0:
        return f"{man:,}만 {rest // 1000}천 원" if man else f"{rest // 1000}천 원"
    return f"약 {round(n / MAN):,}만 원" if n >= MAN else f"{n:,}원"


def _scenario(title: str, f: dict, v: dict, size: int, income_won: int | None, asset_won: int | None, deps: int) -> dict:
    lines, reasons, verdict = [], [], "해당 가능"
    age = v["age"]
    median = f["median_income_monthly_won"].get(str(size))
    youth = f["age_min"] <= age <= f["youth_age_max"]
    general_ok = v["worked"] == "예"
    if not f["age_min"] <= age <= f["age_max"]:
        return {"title": title, "verdict": "해당 어려움", "amount_won": None, "lines": [], "reasons": [f"나이가 지원 연령({f['age_min']}~{f['age_max']}세)을 벗어나요."]}
    if asset_won is None or income_won is None:
        return {"title": title, "verdict": "확인 필요", "amount_won": None, "lines": [],
                "reasons": ["소득·재산 값을 입력하면 계산할 수 있어요."]
                           + ([f"청년 특례 기준: 월소득 {won(int(median * f['youth_income_pct'] / 100))} 이하, 재산 {won(f['youth_asset_max_won'])} 이하"] if median else [])}
    tracks = []                                   # 충족한 트랙 이름
    if youth:
        if asset_won > f["youth_asset_max_won"]:
            reasons.append(f"재산 {won(asset_won)}이 청년 특례 기준({won(f['youth_asset_max_won'])} 이하)을 넘어요.")
        elif median is None:
            reasons.append(f"{size}인 가구의 기준중위소득이 규칙에 없어 소득 기준은 확인하지 못했어요.")
            verdict = "확인 필요"
        else:
            limit = int(median * f["youth_income_pct"] / 100)
            if income_won <= limit:
                tracks.append(f"Ⅰ유형 청년 특례 (소득 {won(income_won)} ≤ 기준 {won(limit)})")
            else:
                reasons.append(f"월소득 {won(income_won)}이 청년 특례 기준(중위소득 {f['youth_income_pct']}% = {won(limit)})을 넘어요.")
    if general_ok:
        if asset_won > f["general_asset_max_won"]:
            reasons.append(f"재산 {won(asset_won)}이 Ⅰ유형 기준({won(f['general_asset_max_won'])} 이하)을 넘어요.")
        elif median is None:
            verdict = "확인 필요"
        else:
            limit = int(median * f["general_income_pct"] / 100)
            if income_won <= limit:
                tracks.append(f"Ⅰ유형 일반 (소득 {won(income_won)} ≤ 기준 {won(limit)})")
            else:
                reasons.append(f"월소득 {won(income_won)}이 Ⅰ유형 기준(중위소득 {f['general_income_pct']}% = {won(limit)})을 넘어요.")
    if not youth and not general_ok:
        reasons.append("취업경험 요건을 채우지 못했고 청년 특례 연령도 아니에요 (Ⅱ유형은 이 계산에 포함하지 않았어요).")
    if not tracks:
        return {"title": title, "verdict": "확인 필요" if verdict == "확인 필요" else "해당 어려움", "amount_won": None, "lines": [], "reasons": reasons}
    add = min(f["dependent_monthly_won"] * deps, f["dependent_monthly_max_won"])
    monthly = f["allowance_monthly_won"] + add
    lines = [f"해당 가능한 유형: {' / '.join(tracks)}",
             f"구직촉진수당 월 {won(f['allowance_monthly_won'])}" + (f" + 부양가족 가산 월 {won(add)} (가족 {deps}명, 최대 월 {won(f['dependent_monthly_max_won'])})" if add else ""),
             f"월 {won(monthly)} × {f['months']}개월 = {won(monthly * f['months'])}"]
    return {"title": title, "verdict": "해당 가능", "amount_won": monthly * f["months"], "lines": lines, "reasons": reasons}


def calculate(rule: dict, v: dict) -> dict:
    """사용자 입력 v(INPUTS의 id -> 값) -> {'scenarios': [...], 'conflicts', 'open_issues', 'sources', 'as_of', 'status'}."""
    f = rule["facts"]
    deps = int(v["dep_minor"]) + int(v["dep_senior"]) + int(v["dep_disabled"])
    scenarios = [
        _scenario("본인이 단독 가구로 인정되는 경우", f, v, 1, int(v["own_income_man"]) * MAN, int(float(v["own_asset_eok"]) * 100_000_000), deps),
        _scenario("가구원과 합산해 심사하는 경우", f, v, int(v["house_size"]), int(v["house_income_man"]) * MAN, int(float(v["house_asset_eok"]) * 100_000_000), deps),
    ]
    notes = []
    if v["restricted"] == "예":
        notes.append("실업급여·생계급여를 받고 있으면 신청이 제한될 수 있어요. 고용센터에 먼저 확인해 주세요.")
    if v["joined_before"] == "있어요":
        notes.append("이전 참여 이력이 있으면 재참여 조건이 달라질 수 있어요. 고용센터에 확인해 주세요.")
    return {"scenarios": scenarios, "notes": notes, "conflicts": rule["conflicts"], "open_issues": rule["open_issues"],
            "sources": rule["sources"], "as_of": rule["as_of"], "status": rule["status"], "general_track_note": rule.get("general_track_note", "")}


AGE_BANDS = ["만 15~34세 (청년)", "만 35~69세"]


def check_flags(rule: dict, items: list[str], checked: set[int]) -> dict[str, bool | None]:
    """카드 체크리스트(items, 번호는 1부터)에서 규칙이 쓰는 조건(check_patterns)의 체크 여부. 그 조건 항목이 카드에 없으면 None."""
    out: dict[str, bool | None] = {}
    for name, pattern in rule.get("check_patterns", {}).items():
        hits = [i for i, text in enumerate(items, start=1) if re.search(pattern, text)]
        out[name] = any(i in checked for i in hits) if hits else None
    return out


def from_checks(rule: dict, flags: dict, age_band: str | None, deps: int) -> dict:
    """체크리스트 답만으로 계산 (소득·재산 숫자를 다시 묻지 않는다). 나이대와 부양가족 수만 추가로 받는다.
    Ⅰ유형과 Ⅱ유형은 중복할 수 없어서 Ⅰ유형(보상이 큼)부터 판정한다: Ⅰ유형이 되면 Ⅰ유형을 추천하고 Ⅱ유형은 선발되지 않을 때의 대안으로,
    Ⅰ유형이 안 되면 Ⅱ유형을 안내한다. 체크는 스스로 확인한 결과이므로 '체크한 조건 기준'이라고 밝힌다."""
    f = rule["facts"]
    youth_tracks = flags.get("income120") and flags.get("asset5")
    general_tracks = flags.get("worked") and flags.get("income60") and flags.get("asset4")
    tracks, reasons = [], []
    if age_band is None:
        reasons.append("나이대를 선택하면 계산해요.")
    else:
        if age_band == AGE_BANDS[0]:
            if youth_tracks:
                tracks.append(f"Ⅰ유형 청년 특례 (중위소득 {f['youth_income_pct']}% 이하·재산 {won(f['youth_asset_max_won'])} 이하 체크)")
            else:
                reasons.append(f"청년 특례는 '중위소득 {f['youth_income_pct']}% 이하'와 '재산 {won(f['youth_asset_max_won'])} 이하'를 모두 체크해야 해요.")
        if general_tracks:
            tracks.append(f"Ⅰ유형 일반 (취업경험·중위소득 {f['general_income_pct']}% 이하·재산 {won(f['general_asset_max_won'])} 이하 체크)")
        else:
            reasons.append("Ⅰ유형 일반은 '취업경험', '중위소득 60% 이하', '재산 4억 이하'를 모두 체크해야 해요.")
    if not tracks:
        verdict = "확인 필요" if age_band is None else "해당 어려움"
        return _checks_result(rule, {"title": "Ⅰ유형 (구직촉진수당) — 체크한 조건 기준", "verdict": verdict, "amount_won": None, "lines": [], "reasons": reasons}, age_band, False)
    add = min(f["dependent_monthly_won"] * deps, f["dependent_monthly_max_won"])
    monthly = f["allowance_monthly_won"] + add
    lines = [f"해당 가능한 유형: {' / '.join(tracks)}",
             f"구직촉진수당 월 {won(f['allowance_monthly_won'])}" + (f" + 부양가족 가산 월 {won(add)} (가족 {deps}명, 최대 월 {won(f['dependent_monthly_max_won'])})" if add else ""),
             f"월 {won(monthly)} × {f['months']}개월 = {won(monthly * f['months'])}"]
    return _checks_result(rule, {"title": "Ⅰ유형 (구직촉진수당) 추천 — 체크한 조건 기준", "verdict": "해당 가능", "amount_won": monthly * f["months"],
                                 "lines": lines, "reasons": []}, age_band, True)


def _checks_result(rule: dict, scenario: dict, age_band: str | None = None, type1_ok: bool = False) -> dict:
    scenarios = [scenario]
    t2 = rule.get("type2")
    if t2 and age_band is not None:
        exclusive = rule.get("exclusive_note", "")
        if type1_ok:              # Ⅰ유형이 되면 Ⅱ유형은 선발되지 않을 때의 대안
            sc = {"title": "Ⅱ유형 (대안)", "verdict": "대안", "reasons": [exclusive, "Ⅰ유형에 선발되지 않으면 Ⅱ유형으로 넘어갈 수 있어요."]}
        elif age_band == AGE_BANDS[0]:      # Ⅰ유형이 안 되면 Ⅱ유형 안내 (청년은 소득·재산·취업경험 무관)
            sc = {"title": "Ⅱ유형 추천 — Ⅰ유형 요건을 채우지 못해요", "verdict": "해당 가능", "reasons": [exclusive]}
        else:                     # 청년이 아니면 Ⅱ유형 대상 여부는 자료로 단정하지 못한다
            sc = {"title": "Ⅱ유형 (참고)", "verdict": "확인 필요", "reasons": [exclusive, "Ⅱ유형 참여 대상인지는 고용센터(☎1350)에서 확인해 주세요."]}
        scenarios.append({**sc, "amount_won": None, "amount_text": t2.get("amount_text", ""), "lines": t2["lines"]})
    return {"scenarios": scenarios, "notes": [], "conflicts": rule["conflicts"], "open_issues": rule["open_issues"], "sources": rule["sources"],
            "as_of": rule["as_of"], "status": rule["status"], "general_track_note": rule.get("general_track_note", "")}


# --- 범용 계산 엔진 (calculator == "generic"): 규칙을 JSON 데이터만으로 표현한다 -----------------------------------------------
# 규칙 형식
#   inputs:   [{id, label, type: int|float|bool|choice, unit_won?(만원 입력이면 10000), min, max, options?, from_check?(체크리스트 문구 정규식)}]
#   variants: [{name, quote(원문 구절), eligibility: [조건...], payout: {...}, adders?: [{input, per_won, max_won?, label}]}]
#   조건: {type: range, input, min?, max?, label} | {type: is_true|is_false, input, label} | {type: in, input, values, label}
#         | {type: median_income, pct, income_input(월소득, unit_won 사용), size_input, label}   (기준중위소득은 MEDIAN_INCOME_2026)
#   payout:  {type: fixed, amount_won, months?, per?} | {type: percent, rate, base_input, cap_won?, months?} | {type: table, by, rows: [{min?, max?, amount_won, label?}], months?}
# 값이 비어 있는(None) 입력이 필요한 조건은 '확인 필요'가 되고, 조건을 하나라도 어기면 '해당 어려움'이다.
MEDIAN_INCOME_2026 = {"1": 2564238, "2": 4199292, "3": 5359036, "4": 6494738}      # 보건복지부 2026년 기준중위소득(월), 5인 이상은 아직 없다
COND_TYPES = ("range", "is_true", "is_false", "in", "median_income")
PAYOUT_TYPES = ("fixed", "percent", "table")
INPUT_TYPES = ("int", "float", "bool", "choice")


def _input_value(spec: dict, values: dict):
    return values.get(spec["id"])


def _check_condition(cond: dict, specs: dict, values: dict, median: dict) -> tuple[bool | None, str]:
    """(충족 여부 True/False/None=값 없음, 설명)."""
    label = cond.get("label") or cond["type"]
    t = cond["type"]
    if t == "median_income":
        income, size = values.get(cond["income_input"]), values.get(cond["size_input"])
        if income is None or size is None:
            return None, f"{label}: 소득·가구원 수를 입력하면 확인해요."
        spec = specs[cond["income_input"]]
        limit_base = median.get(str(int(size)))
        if limit_base is None:
            return None, f"{label}: {int(size)}인 가구의 기준중위소득이 없어 확인하지 못했어요."
        limit = int(limit_base * cond["pct"] / 100)
        mine = int(income * spec.get("unit_won", 1))
        return mine <= limit, f"{label}: 월소득 {won(mine)} (기준 {won(limit)} 이하)"
    spec = specs[cond["input"]]
    v = _input_value(spec, values)
    if v is None:
        return None, f"{label}: 값을 입력하면 확인해요."
    if t == "range":
        ok = (cond.get("min") is None or v >= cond["min"]) and (cond.get("max") is None or v <= cond["max"])
        return ok, f"{label} (입력 {v:g})"
    if t == "is_true":
        return bool(v), label
    if t == "is_false":
        return not v, label
    return v in cond["values"], f"{label} (입력 {v})"


def _payout(variant: dict, specs: dict, values: dict) -> tuple[int | None, list[str], str]:
    """(총액 또는 None, 계산 줄, 모자란 입력 설명)."""
    p, lines = variant["payout"], []
    months = p.get("months")
    per = p.get("per", "월")
    if p["type"] == "fixed":
        unit_amount = p["amount_won"]
    elif p["type"] == "percent":
        base = values.get(p["base_input"])
        if base is None:
            return None, [], f"'{specs[p['base_input']]['label']}'를 입력하면 금액을 계산해요."
        raw = int(base * specs[p["base_input"]].get("unit_won", 1))
        unit_amount = int(raw * p["rate"])
        lines.append(f"{won(raw)}의 {p['rate'] * 100:g}% = {won(unit_amount)}")
        if p.get("cap_won") is not None and unit_amount > p["cap_won"]:
            unit_amount = p["cap_won"]
            lines.append(f"한도 {won(p['cap_won'])} 적용")
    else:
        v = values.get(p["by"])
        if v is None:
            return None, [], f"'{specs[p['by']]['label']}'를 입력하면 금액을 계산해요."
        row = next((r for r in p["rows"] if (r.get("min") is None or v >= r["min"]) and (r.get("max") is None or v <= r["max"])), None)
        if row is None:
            return None, [], "입력한 값에 해당하는 금액 구간이 규칙에 없어요."
        unit_amount = row["amount_won"]
        row_name = row.get("label") or "%s %g" % (specs[p["by"]]["label"], v)
        lines.append(f"{row_name} → {won(unit_amount)}")
    add_total = 0
    for a in variant.get("adders", []):
        n = values.get(a["input"])
        if n:
            add = int(n * a["per_won"])
            add = min(add, a["max_won"]) if a.get("max_won") is not None else add
            add_total += add
            lines.append(f"{a.get('label', '가산')} +{won(add)}")
    unit_total = unit_amount + add_total
    if months:
        return unit_total * months, lines + [f"{per} {won(unit_total)} × {months}{'개월' if per == '월' else '회'} = {won(unit_total * months)}"], ""
    return unit_total, lines + [f"{won(unit_total)}"], ""


def generic_calculate(rule: dict, values: dict) -> dict:
    """범용 규칙 계산. values: {입력 id -> 값 또는 None}. calculate()와 같은 모양으로 돌려줘 화면이 그대로 쓴다."""
    specs = {s["id"]: s for s in rule["inputs"]}
    median = (rule.get("facts") or {}).get("median_income_monthly_won") or MEDIAN_INCOME_2026
    scenarios = []
    for variant in rule["variants"]:
        results = [_check_condition(c, specs, values, median) for c in variant.get("eligibility", [])]
        failed = [text for ok, text in results if ok is False]
        unknown = [text for ok, text in results if ok is None]
        if failed:
            scenarios.append({"title": variant["name"], "verdict": "해당 어려움", "amount_won": None, "lines": [],
                              "reasons": [f"충족하지 못한 조건 — {t}" for t in failed]})
            continue
        total, lines, missing = _payout(variant, specs, values)
        if unknown or total is None:
            scenarios.append({"title": variant["name"], "verdict": "확인 필요", "amount_won": None, "lines": [],
                              "reasons": unknown + ([missing] if missing else [])})
            continue
        scenarios.append({"title": variant["name"], "verdict": "해당 가능", "amount_won": total,
                          "lines": [f"충족한 조건: {', '.join(t for _, t in results)}"] * bool(results) + lines, "reasons": []})
    order = {"해당 가능": 0, "확인 필요": 1, "해당 어려움": 2}
    scenarios.sort(key=lambda s: (order[s["verdict"]], -(s["amount_won"] or 0)))
    eligible = [s for s in scenarios if s["verdict"] == "해당 가능"]
    if len(eligible) > 1:
        eligible[0]["lines"].append("해당하는 유형 중 금액이 가장 커요. 유형끼리 중복해서 받을 수 있는지는 담당 기관에서 확인해 주세요.")
    return {"scenarios": scenarios, "notes": [], "conflicts": rule.get("conflicts", []), "open_issues": rule.get("open_issues", []),
            "sources": rule["sources"], "as_of": rule["as_of"], "status": rule["status"], "general_track_note": rule.get("general_track_note", "")}


def auto_flags(rule: dict, items: list[str], checked: set[int]) -> dict:
    """범용 규칙의 입력 중 from_check가 있는 것을 체크리스트 답으로 채운다 -> {입력 id: 값}. 카드에 그 항목이 없으면 채우지 않는다."""
    out = {}
    for spec in rule["inputs"]:
        pat = spec.get("from_check")
        hits = [i for i, text in enumerate(items, start=1) if pat and re.search(pat, text)]
        if hits:
            out[spec["id"]] = any(i in checked for i in hits)
    return out

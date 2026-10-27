"""제도별 '예상 수령액' 계산 (국민취업지원제도부터).

규칙(금액·기간·기준·출처·확인일)은 `amount_rules.json`에 사람이 정해 두고, 사용자가 입력한 값으로 금액을 코드가 계산한다 (LLM 미사용, 같은
입력이면 같은 결과). 시나리오(본인 단독 가구 / 가구 합산)를 나눠 보여 주고, 규칙으로 판단하지 못하는 것은 '남은 변수'로 돌려준다.
계산 값은 참고용이며, 규칙에 없는 제도는 `find`가 None을 돌려줘 카드가 기존 방식 그대로 동작한다."""
from __future__ import annotations

import json
import re
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


@lru_cache(maxsize=1)
def _load() -> list[dict]:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))["rules"]


def find(service_id: str) -> dict | None:
    return next((r for r in _load() if service_id and service_id in r["service_ids"]), None)


def inputs_for(rule: dict) -> list[dict]:
    return INPUTS[rule["calculator"]]


def won(n: int) -> str:
    """원 -> '6억 원' / '360만 원' / '약 308만 원' / '1,234원'."""
    if n % 100_000_000 == 0 and n >= 100_000_000:
        return f"{n // 100_000_000:,}억 원"
    if n % MAN == 0:
        return f"{n // MAN:,}만 원"
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
    체크는 스스로 확인한 결과이므로 결과 문구도 '체크한 조건 기준'이라고 밝힌다. 모르는(카드에 없는) 조건은 단정하지 않는다."""
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
        return _checks_result(rule, {"title": "체크한 조건 기준", "verdict": verdict, "amount_won": None, "lines": [], "reasons": reasons})
    add = min(f["dependent_monthly_won"] * deps, f["dependent_monthly_max_won"])
    monthly = f["allowance_monthly_won"] + add
    lines = [f"해당 가능한 유형: {' / '.join(tracks)}",
             f"구직촉진수당 월 {won(f['allowance_monthly_won'])}" + (f" + 부양가족 가산 월 {won(add)} (가족 {deps}명, 최대 월 {won(f['dependent_monthly_max_won'])})" if add else ""),
             f"월 {won(monthly)} × {f['months']}개월 = {won(monthly * f['months'])}"]
    return _checks_result(rule, {"title": "체크한 조건 기준", "verdict": "해당 가능", "amount_won": monthly * f["months"], "lines": lines, "reasons": []})


def _checks_result(rule: dict, scenario: dict) -> dict:
    return {"scenarios": [scenario], "notes": [], "conflicts": rule["conflicts"], "open_issues": rule["open_issues"], "sources": rule["sources"],
            "as_of": rule["as_of"], "status": rule["status"], "general_track_note": rule.get("general_track_note", "")}

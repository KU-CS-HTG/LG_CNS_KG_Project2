"""근로·자녀장려금 '예상 수령액' 계산 (`amount_rules`와 같은 방식: 규칙은 JSON에, 계산은 코드가, LLM 없음).

사용자가 가구원 구성(배우자·부양자녀·70세 이상 부양 직계존속)과 총급여액을 넣으면 국세청의 가구 유형(단독·홑벌이·맞벌이)을 정하고
근로장려금을 산식으로, 자녀장려금을 범위로 계산한다. 금액을 정하는 값(최대 지급액·구간 경계·소득 상한·재산 기준)은 모두
`earned_credit_rules.json`에 있고 공식 원문 대조 전 '초안'이다. 자료마다 다른 기준(예: 맞벌이 소득 상한)은 하나를 고르지 않고 후보마다
계산해 범위로 돌려준다. 규칙에 없는 제도는 `find`가 None을 돌려줘 카드가 기존 방식 그대로 동작한다.

산식 (총급여액등 x, 근로장려금): x < 구간1 끝: 최대액 × x / 구간1 끝 / 최대 구간(구간1 끝~구간2 끝): 최대액 /
구간2 끝~소득 상한: 최대액 × (상한 - x) / (상한 - 구간2 끝) (직선 감액). 재산 1.7억 이상 2.4억 미만이면 산정액의 50%, 2.4억 이상이면 0."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from src.generate.amount_rules import won

RULES_PATH = Path(__file__).with_name("earned_credit_rules.json")
MAN = 10_000

INPUTS = [
    {"id": "own_man", "label": "본인 연간 총급여액 (만원)", "type": "int", "min": 0, "max": 100_000},
    {"id": "spouse", "label": "배우자가 있나요?", "type": "choice", "options": ["예", "아니요"]},
    {"id": "spouse_man", "label": "배우자 연간 총급여액 (만원, 없으면 0)", "type": "int", "min": 0, "max": 100_000, "when": {"spouse": "예"}},
    {"id": "children", "label": "만 18세 미만 부양자녀 수 (명)", "type": "int", "min": 0, "max": 10},
    {"id": "seniors", "label": "만 70세 이상 부양 직계존속 수 (명)", "type": "int", "min": 0, "max": 10},
    {"id": "asset_man", "label": "가구원 전체 재산 합계 (만원, 모르면 비워 두세요)", "type": "int", "min": 0, "max": 10_000_000, "optional": True},
]


@lru_cache(maxsize=4)
def _read(mtime_ns: int) -> list[dict]:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))["rules"]


def _load() -> list[dict]:
    """규칙 파일 내용. 파일이 바뀌면(수정 시각이 달라지면) 앱을 다시 띄우지 않아도 새 값을 읽는다."""
    return _read(RULES_PATH.stat().st_mtime_ns)


def find(service_id: str) -> dict | None:
    return next((r for r in _load() if service_id and service_id in r["service_ids"]), None)


def household_type(own_won: int, spouse: bool, spouse_won: int, children: int, seniors: int, min_each_won: int) -> str:
    """국세청 기준: 맞벌이 = 신청인과 배우자 각각 총급여액등 300만 원 이상 / 홑벌이 = 배우자(총급여 300만 원 미만)나 부양자녀·70세 이상
    직계존속이 있는 가구 / 단독 = 그 밖."""
    if spouse and own_won >= min_each_won and spouse_won >= min_each_won:
        return "two_earner"
    if spouse or children > 0 or seniors > 0:
        return "one_earner"
    return "single"


def work_credit(spec: dict, income_won: int, cap_won: int) -> int:
    """근로장려금(재산 감액 전, 원). 소득 상한 이상이거나 소득이 없으면 0."""
    if income_won <= 0 or income_won >= cap_won:
        return 0
    mx, r1, r2 = spec["max_won"], spec["ramp_end_won"], spec["plateau_end_won"]
    if income_won < r1:
        amount = mx * income_won / r1
    elif income_won < r2:
        amount = mx
    else:
        amount = mx * (cap_won - income_won) / (cap_won - r2)
    return int(round(amount, -1))           # 10원 단위


def asset_factor(rule: dict, asset_won: int | None) -> tuple[float, str | None]:
    """재산에 따른 지급 비율 (1.0 / 0.5 / 0.0)과 설명. 재산을 모르면 1.0이고 설명에 확인 필요를 적는다."""
    w = rule["work"]
    if asset_won is None:
        return 1.0, "재산을 입력하지 않아 재산 요건(2억 4천만 원 미만, 1억 7천만 원 이상이면 50%만 지급)은 반영하지 않았어요."
    if asset_won >= w["asset_cap_won"]:
        return 0.0, f"재산 합계가 {won(w['asset_cap_won'])} 이상이라 받기 어려워요."
    if asset_won >= w["asset_half_from_won"]:
        return 0.5, f"재산 합계가 {won(w['asset_half_from_won'])} 이상 {won(w['asset_cap_won'])} 미만이라 산정액의 50%만 지급돼요."
    return 1.0, None


def calculate(rule: dict, v: dict) -> dict:
    """입력 -> {household, verdict, work, child, notes, conflicts, open_issues, sources, as_of, status}.

    work: {title, verdict, low_won, high_won, lines}. 후보 소득 상한이 여러 개면 low~high 범위(같으면 low == high).
    child: {verdict, low_won, high_won, lines} 또는 None(부양자녀가 없거나 단독 가구)."""
    w, c = rule["work"], rule["child"]
    own, spouse_won = int(v.get("own_man") or 0) * MAN, int(v.get("spouse_man") or 0) * MAN
    spouse = v.get("spouse") == "예"
    children, seniors = int(v.get("children") or 0), int(v.get("seniors") or 0)
    asset_man = v.get("asset_man")
    asset_won = None if asset_man is None else int(asset_man) * MAN
    htype = household_type(own, spouse, spouse_won if spouse else 0, children, seniors, w["two_earner_min_each_won"])
    spec = w[htype]
    income = own + (spouse_won if spouse else 0)
    factor, asset_note = asset_factor(rule, asset_won)
    notes: list[str] = [asset_note] if asset_note else []

    caps = spec["income_cap_candidates"]
    amounts = sorted({int(round(work_credit(spec, income, cap) * factor, -1)) for cap in caps})
    low, high = amounts[0], amounts[-1]
    lines = [f"가구 유형: {spec['label']} (총급여액등 합계 {won(income)})"]
    if income <= 0:
        verdict = "해당 어려움"
        lines.append("근로·사업·종교인소득이 있어야 받을 수 있어요.")
    elif factor == 0.0:
        verdict = "해당 어려움"
    elif high == 0:
        verdict = "해당 어려움"
        lines.append(f"소득이 기준({'/'.join(won(x) for x in caps)} 미만)을 넘어요.")
    else:
        verdict = "확인 필요" if low != high or low == 0 else "해당 가능"
        if low != high:
            lines.append(f"소득 상한이 자료마다 달라({' vs '.join(won(x) for x in caps)}) 금액이 {won(low)} ~ {won(high)}로 나와요. 공식 모의계산으로 확인해 주세요.")
        if factor == 0.5:
            lines.append("재산 요건에 따른 50% 감액을 반영했어요.")
    work = {"title": "근로장려금", "verdict": verdict, "low_won": low, "high_won": high, "lines": lines}

    child = None
    if children > 0 and htype != "single":
        cl: list[str] = []
        if income >= c["income_cap_won"] or factor == 0.0:
            cv, clow, chigh = "해당 어려움", 0, 0
            cl.append(f"부부합산 총소득이 {won(c['income_cap_won'])} 미만이고 재산이 기준 미만이어야 해요.")
        else:
            cv = "확인 필요"
            clow = int(round(children * c["per_child_min_won"] * factor, -1))
            chigh = int(round(children * c["per_child_max_won"] * factor, -1))
            cl.append(f"부양자녀 {children}명 × 1인당 {won(c['per_child_min_won'])}~{won(c['per_child_max_won'])} (소득이 낮을수록 많아요).")
            cl.append("소득 구간별 정확한 금액은 산정표(공식 모의계산)로 확인해야 해요.")
            if factor == 0.5:
                cl.append("재산 요건에 따른 50% 감액을 반영했어요.")
        child = {"title": "자녀장려금", "verdict": cv, "low_won": clow, "high_won": chigh, "lines": cl}
    elif children > 0:
        child = None
    return {"household": htype, "household_label": spec["label"], "work": work, "child": child, "notes": notes,
            "conflicts": list(rule.get("conflicts") or []), "open_issues": list(rule.get("open_issues") or []),
            "sources": list(rule.get("sources") or []), "as_of": rule["as_of"], "status": rule["status"]}

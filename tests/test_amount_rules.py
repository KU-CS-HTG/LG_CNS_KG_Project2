"""제도별 예상 수령액 계산(amount_rules): 규칙 파일 형식, 계산 결과, 카드 화면."""
import json

import pytest

from src.generate import amount_rules as ar

SID = "149200005007"


def base(**kw):
    v = {i["id"]: i["default"] for i in ar.inputs_for(ar.find(SID))}
    v.update(kw)
    return v


def test_rules_file_has_sources_and_required_facts():
    for rule in json.loads(ar.RULES_PATH.read_text(encoding="utf-8"))["rules"]:
        assert rule["service_ids"] and rule["as_of"] and rule["sources"] and rule["calculator"] in ar.INPUTS
        assert all(s["url"].startswith("https://") and s["level"] and s["facts"] for s in rule["sources"])
        assert {"allowance_monthly_won", "months", "median_income_monthly_won"} <= set(rule["facts"])
    assert ar.find("없는 제도") is None and ar.find("") is None


def test_single_household_young_applicant_gets_360만_and_합산_fails_on_assets():
    out = ar.calculate(ar.find(SID), base(age=26, house_size=4, house_income_man=583, house_asset_eok=6.0, own_income_man=100, own_asset_eok=0.5))
    single, joint = out["scenarios"]
    assert single["verdict"] == "해당 가능" and single["amount_won"] == 3_600_000
    assert "월 60만 원 × 6개월 = 360만 원" in single["lines"][-1]
    assert joint["verdict"] == "해당 어려움" and joint["amount_won"] is None and "재산" in joint["reasons"][0]


def test_dependents_add_up_to_the_monthly_cap():
    out = ar.calculate(ar.find(SID), base(age=30, own_income_man=100, own_asset_eok=0.1, dep_minor=2, dep_senior=1, dep_disabled=3))
    assert out["scenarios"][0]["amount_won"] == (600_000 + 400_000) * 6                       # 가산은 월 40만 원까지
    out = ar.calculate(ar.find(SID), base(age=30, own_income_man=100, own_asset_eok=0.1, dep_minor=1))
    assert out["scenarios"][0]["amount_won"] == 700_000 * 6


def test_income_over_limit_and_age_and_unknown_household_size():
    over = ar.calculate(ar.find(SID), base(age=30, own_income_man=400, own_asset_eok=0.1))["scenarios"][0]
    assert over["verdict"] == "해당 어려움" and "월소득" in over["reasons"][0]
    old = ar.calculate(ar.find(SID), base(age=75, own_income_man=0))["scenarios"][0]
    assert old["verdict"] == "해당 어려움" and old["amount_won"] is None
    big = ar.calculate(ar.find(SID), base(age=30, house_size=5, house_income_man=300, house_asset_eok=1.0))["scenarios"][1]
    assert big["verdict"] == "확인 필요" and big["amount_won"] is None                       # 5인 기준중위소득은 규칙에 없다 -> 단정하지 않는다


def test_non_youth_needs_work_history_and_general_track_uses_60_percent():
    no_work = ar.calculate(ar.find(SID), base(age=45, worked="아니요", own_income_man=50, own_asset_eok=0.1))["scenarios"][0]
    assert no_work["amount_won"] is None
    ok = ar.calculate(ar.find(SID), base(age=45, worked="예", own_income_man=100, own_asset_eok=0.1))["scenarios"][0]
    assert ok["amount_won"] == 3_600_000                                                       # 1인 중위소득 60% = 약 153.8만 원
    high = ar.calculate(ar.find(SID), base(age=45, worked="예", own_income_man=200, own_asset_eok=0.1))["scenarios"][0]
    assert high["amount_won"] is None


def test_restriction_and_open_issues_are_always_reported():
    out = ar.calculate(ar.find(SID), base(age=26, restricted="예", joined_before="있어요"))
    assert len(out["notes"]) == 2 and out["open_issues"] and out["conflicts"] and out["sources"]


def test_from_checks_uses_checklist_answers_without_asking_numbers_again():
    rule = ar.find(SID)
    items = ["연령 — 15세~69세 구직자 중", "소득 — 중위소득 60% 이하", "재산 — 재산 4억 이하", "취업 경험 — 최근 2년 이내 100일 또는 800시간 이상의 취업경험",
             "소득 — 중위소득 120% 이하", "재산 — 재산 5억원 이하"]
    flags = ar.check_flags(rule, items, {1, 2, 3, 5, 6})
    assert flags == {"income60": True, "asset4": True, "worked": False, "income120": True, "asset5": True}
    youth = ar.from_checks(rule, flags, ar.AGE_BANDS[0], 1)["scenarios"][0]
    assert youth["verdict"] == "해당 가능" and youth["amount_won"] == 700_000 * 6
    assert [x["title"] for x in ar.from_checks(rule, flags, ar.AGE_BANDS[0], 0)["scenarios"]] == ["Ⅰ유형 (구직촉진수당) 추천 — 체크한 조건 기준", "Ⅱ유형 (대안)"]   # Ⅱ유형은 금액 없이 안내
    older = ar.from_checks(rule, flags, ar.AGE_BANDS[1], 0)["scenarios"][0]                  # 35세 이상은 청년 특례 불가, 취업경험도 체크 안 함
    assert older["verdict"] == "해당 어려움" and older["amount_won"] is None
    assert ar.from_checks(rule, flags, None, 0)["scenarios"][0]["verdict"] == "확인 필요"
    none = ar.check_flags(rule, ["연령 — 15세~69세"], {1})
    assert none["income60"] is None and ar.from_checks(rule, none, ar.AGE_BANDS[0], 0)["scenarios"][0]["amount_won"] is None


def test_type2_is_alternative_when_type1_ok_and_recommended_when_not():
    rule = ar.find(SID)
    ok = ar.from_checks(rule, {"income120": True, "asset5": True}, ar.AGE_BANDS[0], 0)["scenarios"]
    assert ok[0]["verdict"] == "해당 가능" and ok[1]["verdict"] == "대안" and any("중복" in x for x in ok[1]["reasons"]) and any("선발되지 않으면" in x for x in ok[1]["reasons"])
    no1 = ar.from_checks(rule, {"income120": False, "asset5": False}, ar.AGE_BANDS[0], 0)["scenarios"]
    assert no1[0]["verdict"] == "해당 어려움" and no1[1]["title"].startswith("Ⅱ유형 추천") and no1[1]["verdict"] == "해당 가능"
    older = ar.from_checks(rule, {}, ar.AGE_BANDS[1], 0)["scenarios"]
    assert older[1]["verdict"] == "확인 필요" and any("고용센터" in x for x in older[1]["reasons"])             # 청년이 아니면 대상 여부를 단정하지 않는다
    assert len(ar.from_checks(rule, {}, None, 0)["scenarios"]) == 1                                               # 나이대 모르면 Ⅱ유형도 안내하지 않는다
    assert any("법률" in s["title"] for s in rule["sources"])


def test_type2_amounts_are_shown_with_secondary_source_caveat():
    rule = ar.find(SID)
    t2 = ar.from_checks(rule, {"income120": True, "asset5": True}, ar.AGE_BANDS[0], 0)["scenarios"][1]
    text = " ".join(t2["lines"])
    assert t2["amount_won"] is None and "최대 25만 원" in t2["amount_text"] and "월 2만 원" in text and "2차 자료" in text
    assert any(s["level"].startswith("2차") for s in rule["sources"])


def test_won_format():
    assert ar.won(3_600_000) == "360만 원" and ar.won(600_000_000) == "6억 원" and ar.won(3_077_085) == "약 308만 원"


def _script():
    import streamlit as st
    from app import main as app
    from src.common.schema import Recommendation

    st.session_state.setdefault("checks", {})
    st.session_state.setdefault("guides", {})
    r = Recommendation("국민취업지원제도", "고용노동부", "조건 확인", "취업지원서비스 및 생계안정 소득 지원", "고용센터", service_id="149200005007",
                       needs_check=["연령 — 15세~69세 구직자 중", "소득 — 중위소득 60% 이하", "재산 — 재산 4억 이하",
                                    "취업 경험 — 최근 2년 이내 100일 또는 800시간 이상의 취업경험", "소득 — 중위소득 120% 이하", "재산 — 재산 5억원 이하"])
    app._card(1, r, "k1")


def test_card_shows_amount_form_and_calculated_result(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    from src.generate import benefit_tiers as bt

    tiers = {"tiers": [{"label": "II유형", "requires": [], "benefit": "참여수당", "quote": "", "basis": "outside", "amount": "", "amount_basis": "", "evidence": []}], "notes": []}
    monkeypatch.setattr(bt, "compute", lambda *a, **k: tiers)
    at = AppTest.from_function(_script, default_timeout=30).run()
    for c in at.checkbox:
        if c.key in {f"chk_k1_{j}" for j in (0, 1, 2, 4, 5)}:                                  # 취업경험만 빼고 체크
            c.check()
    next(b for b in at.button if b.label == "체크 완료").click().run()
    next(b for b in at.button if "내 조건으로" in b.label).click().run()
    assert not at.exception and any("예상 수령액" in m.value for m in at.markdown)
    assert not any("금액을 확인하지 못했어요" in c.value for c in at.caption)               # 규칙이 있는 제도는 항목별 '확인 못 함' 문구를 쓰지 않는다
    assert any("예상 수령액" in m.value for m in at.markdown) and at.radio(key="amt_band_k1").value is None
    assert not any("예상 수령액: " in m.value for m in at.markdown)                           # 나이대를 고르기 전에는 금액을 단정하지 않는다
    at.radio(key="amt_band_k1").set_value("만 15~34세 (청년)").run()
    text = " ".join(m.value for m in at.markdown)
    assert "예상 수령액: 360만 원" in text and "청년 특례" in text                              # 체크한 조건만으로 바로 계산 (다시 묻지 않음)
    at.number_input(key="amt_k1_minor").set_value(2).run()
    assert "예상 수령액: 480만 원" in " ".join(m.value for m in at.markdown)                    # (60만+20만)×6
    next(b for b in at.button if b.label == "예상 금액 계산").click().run()
    assert not at.exception and "가구원과 합산해 심사하는 경우" in " ".join(m.value for m in at.markdown)
    assert any("자세히 보기 (계산 근거" in e.label for e in at.expander)

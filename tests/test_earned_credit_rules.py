"""근로·자녀장려금 예상 수령액 계산 (earned_credit_rules): 가구 유형 판정·산식·재산 감액·자료 불일치 범위."""
import pytest

from src.generate import earned_credit_rules as ec

RULE = ec.find("105100000001")


def calc(**kw):
    v = {"own_man": 0, "spouse": "아니요", "spouse_man": 0, "children": 0, "seniors": 0, "asset_man": None}
    v.update(kw)
    return ec.calculate(RULE, v)


def test_rule_is_found_only_for_the_service():
    assert RULE and RULE["status"].startswith("초안") and ec.find("nope") is None and ec.find("") is None


@pytest.mark.parametrize("kw,label", [
    ({"own_man": 1000}, "단독 가구"),
    ({"own_man": 1000, "children": 1}, "홑벌이 가구"),                                  # 배우자 없이 부양자녀가 있으면 홑벌이
    ({"own_man": 1000, "seniors": 1}, "홑벌이 가구"),
    ({"own_man": 1000, "spouse": "예", "spouse_man": 200}, "홑벌이 가구"),             # 배우자 총급여 300만 원 미만
    ({"own_man": 1000, "spouse": "예", "spouse_man": 300}, "맞벌이 가구"),            # 각각 300만 원 이상
    ({"own_man": 200, "spouse": "예", "spouse_man": 900}, "홑벌이 가구"),              # 본인이 300만 원 미만이면 맞벌이 아님
])
def test_household_type(kw, label):
    assert calc(**kw)["household_label"] == label


@pytest.mark.parametrize("kw,expected", [
    ({"own_man": 300}, 1_237_500),                      # 단독: 300 × 165/400
    ({"own_man": 600}, 1_650_000),                      # 최대 구간
    ({"own_man": 1550}, 825_000),                       # 165 - (1550-900) × 165/1300
    ({"own_man": 2200}, 0),                             # 소득 상한 이상
    ({"own_man": 1500, "spouse": "예", "spouse_man": 0}, 2_691_670),                    # 홑벌이: 285 - (1500-1400) × 285/1800 (만원 → 10원 단위 반올림)
    ({"own_man": 1000, "spouse": "예", "spouse_man": 0}, 2_850_000),
])
def test_work_credit_formula(kw, expected):
    w = calc(**kw)["work"]
    assert w["low_won"] == w["high_won"] == expected


def test_two_earner_over_the_plateau_shows_range_for_conflicting_income_caps():
    w = calc(own_man=1500, spouse="예", spouse_man=300)["work"]            # 합계 1,800만 원: 상한 3,800 vs 4,400 후보
    assert (w["low_won"], w["high_won"]) == (3_142_860, 3_177_780) and w["verdict"] == "확인 필요" and "자료마다 달라" in " ".join(w["lines"])
    same = calc(own_man=500, spouse="예", spouse_man=500)["work"]           # 1,000만 원: 최대 구간이라 두 후보가 같다
    assert same["low_won"] == same["high_won"] == 3_300_000 and same["verdict"] == "해당 가능"
    mid = calc(own_man=2000, spouse="예", spouse_man=1800)["work"]          # 3,800만 원: 한 후보는 상한 이상(0), 다른 후보는 지급
    assert mid["low_won"] == 0 and mid["high_won"] > 0 and mid["verdict"] == "확인 필요"


def test_asset_rules():
    assert calc(own_man=1000, asset_man=20_000)["work"]["low_won"] == 761_540                   # 2억 원: 50% 감액 (1,523,080 × 0.5)
    out = calc(own_man=1000, asset_man=25_000)
    assert out["work"]["low_won"] == 0 and out["work"]["verdict"] == "해당 어려움" and "받기 어려워요" in " ".join(out["notes"])
    assert "재산을 입력하지 않아" in " ".join(calc(own_man=1000)["notes"])                      # 모르면 반영하지 않았다고 알린다


def test_no_income_is_not_eligible():
    assert calc(own_man=0)["work"]["verdict"] == "해당 어려움"


def test_child_credit_is_a_range_per_child_and_needs_a_child_and_non_single_household():
    out = calc(own_man=1000, spouse="예", spouse_man=0, children=2)
    assert (out["child"]["low_won"], out["child"]["high_won"]) == (1_000_000, 2_000_000) and out["child"]["verdict"] == "확인 필요"
    assert calc(own_man=1000)["child"] is None                                                  # 부양자녀 없음
    assert calc(own_man=1000, children=1)["child"]["low_won"] == 500_000                        # 배우자 없어도 부양자녀 있으면 홑벌이
    rich = calc(own_man=5000, spouse="예", spouse_man=2500, children=1)["child"]                 # 합계 7,500만 원
    assert rich["verdict"] == "해당 어려움" and rich["high_won"] == 0
    half = calc(own_man=1000, spouse="예", spouse_man=0, children=2, asset_man=20_000)["child"]
    assert (half["low_won"], half["high_won"]) == (500_000, 1_000_000)


def test_result_always_lists_sources_conflicts_and_open_issues():
    out = calc(own_man=1000)
    assert out["sources"] and out["conflicts"] and out["open_issues"] and out["status"].startswith("초안")


def test_card_shows_calculator_even_when_llm_summary_fails(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    from src.generate import benefit_tiers
    monkeypatch.setattr(benefit_tiers, "compute", lambda *a, **k: None)

    def script():
        import streamlit as st
        from app import main as app
        from src.common.schema import Recommendation
        st.session_state.setdefault("checks", {})
        st.session_state.setdefault("guides", {})
        app._card(1, Recommendation("근로·자녀장려금", "국세청", "이유", "혜택", "신청", needs_check=["a"], service_id="105100000001"), "t1")
    at = AppTest.from_function(script, default_timeout=30).run()
    at.checkbox[0].check().run()
    next(b for b in at.button if b.label == "체크 완료").click().run()
    next(b for b in at.button if "내 조건으로" in b.label).click().run()
    assert not at.exception and any("예상 수령액 (근로·자녀장려금)" in m.value for m in at.markdown)
    at.number_input(key="ecr_t1_own_man").set_value(1000).run()
    at.radio(key="ecr_t1_spouse").set_value("아니요").run()
    text = " ".join(m.value for m in at.markdown)
    assert not at.exception and "단독 가구" in text and "예상 수령액" in text and "152만" in text.replace(",", "")

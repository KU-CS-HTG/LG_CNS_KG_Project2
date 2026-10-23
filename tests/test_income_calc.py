"""C: 소득 기준 계산기 (src/generate/income_calc.py, 사이드바·카드 표시). 기대값은 2026년 판정 기준 문서의 공식 표."""
import pytest

from src.generate import income_calc as calc


def test_median_and_amounts_match_official_2026_tables():
    assert calc.median(1) == 2_564_238 and calc.median(4) == 6_494_738
    assert calc.median(8) == 10_474_348            # 사업안내 p.14 '8인가구：10,474,348원' (1인당 959,198원 증가)
    assert calc.amount(1, 32) == 820_556           # 생계급여 1인 선정기준
    assert calc.amount(2, 32) == 1_343_773
    assert calc.amount(1, 48) == 1_230_834         # 주거급여 1인 선정기준 (원 단위 버림)
    assert calc.percent(1, 2_000_000) == 78.0
    with pytest.raises(ValueError):
        calc.median(0)


def test_threshold_reads_only_one_median_income_percent():
    assert calc.threshold("소득 — 기준 중위소득 60% 이하") == (60.0, False)
    assert calc.threshold("소득 — 가구단위 월평균 총소득이 기준 중위소득의 100분의 120 이하") == (120.0, False)
    assert calc.threshold("소득 — 중위소득 150%미만") == (150.0, True)
    assert calc.threshold("소득 — 중위소득 60% 이하(청년은 120% 이하)") == (60.0, False)   # 괄호 안은 '중위소득' 없이 비율만
    assert calc.threshold("소득 — 중위소득 60% 이하, 청년은 중위소득 120% 이하") is None  # 대상별로 달라 비교하지 않음
    assert calc.threshold("소득 — 본인+부모 도시근로자 월평균소득 100% 이하") is None       # 다른 기준
    assert calc.threshold("소득 — 본인 연소득 6천만원 이하") is None
    assert calc.threshold("주택 — 보증금 5천만원 이하") is None


def test_check_compares_without_asserting_eligibility():
    c = calc.check("소득 — 기준 중위소득 60% 이하", 4, 3_000_000)
    assert (c.limit, c.mine, c.limit_amount, c.within) == (60.0, 46.2, 3_896_842, True)
    assert not calc.check("소득 — 기준 중위소득 32% 이하", 1, 900_000).within
    assert not calc.check("소득 — 중위소득 50% 미만", 1, calc.amount(1, 50) + 1).within
    assert calc.check("소득 — 기준 중위소득 60% 이하", None, 3_000_000) is None   # 입력이 없으면 비교하지 않음
    assert calc.check("주택 — 보증금 5천만원 이하", 4, 3_000_000) is None
    assert calc.won(2_564_238) == "256만 4,238원" and calc.won(5_000_000) == "500만원" and calc.won(820) == "820원"


def _calc_script():
    import streamlit as st

    from app import main as app
    from src.common.schema import Answer, Recommendation

    st.session_state.setdefault("checks", {})
    st.session_state.setdefault("guides", {})
    app._income_calculator()
    rec = Recommendation(service_name="국민취업지원제도", agency="고용노동부", reason="이유", benefit="수당",
                         how_to_apply="온라인 신청", needs_check=["소득 — 기준 중위소득 60% 이하", "재산 — 4억원 이하"])
    app._render_answer(Answer(text="", recommendations=[rec]), key="0_1")


def test_app_calculator_shows_percent_and_card_hint_only_after_input():
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_calc_script).run()
    hints = lambda: [m.value for m in at.markdown if "calc-hint" in m.value]  # noqa: E731
    assert hints() == []                                   # 소득을 넣기 전에는 카드에 아무것도 안 붙는다
    assert any("중위소득 32%" in m.value and "82만 556원" in m.value for m in at.markdown)   # 1인 기본: 표만

    at.selectbox(key="calc_household").set_value(4)
    at.number_input(key="calc_income_man").set_value(300).run()
    assert any("46.2%" in m.value and "calc-big" in m.value for m in at.markdown)
    [hint] = hints()                                       # '재산' 항목에는 붙지 않는다
    assert "46.2%" in hint and "4인 가구 월 389만 6,842원" in hint and "기준 안쪽" in hint and "참고용" in hint
    assert not any(c.value for c in at.checkbox)           # 체크는 사용자가 한다 (계산기가 대신 체크하지 않음)

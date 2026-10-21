"""C: UI(app/main.py) 자격 체크리스트 테스트. 실제 검색·LLM 없이 Answer를 직접 그린다."""
from __future__ import annotations

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app import main as app  # noqa: E402


def test_badge_follows_checked_count():
    assert "해당 가능성 높음" in app._badge(0, 0)
    assert "확인 필요 3건" in app._badge(3, 0)
    assert "3건 중 2건 체크" in app._badge(3, 2)
    assert "체크한 조건 모두 충족" in app._badge(3, 3)


def test_check_label_bolds_condition_name_and_escapes_markdown():
    assert app._check_label("소득 — 본인 연소득 6천만원 이하") == "**소득** — 본인 연소득 6천만원 이하"
    assert app._check_label("자격 요건 충족 여부") == "자격 요건 충족 여부"
    assert app._check_label("재산 — 3억 4,500만원 이하*") == r"**재산** — 3억 4,500만원 이하\*"


def _script():
    import streamlit as st

    from app import main as app
    from src.common.schema import Answer, Citation, Recommendation

    st.session_state.setdefault("checks", {})
    rec = Recommendation(service_name="청년월세 지원", agency="국토교통부", reason="이유", benefit="월세 지원",
                         how_to_apply="온라인 신청", needs_check=["소득 — 중위소득 60% 이하", "주택 조건 — 보증금 5천만원 이하"],
                         citations=[Citation(service_name="청년월세 지원", agency="국토교통부", quote="근거",
                                             detail_url="https://www.gov.kr")])
    app._render_answer(Answer(text="", recommendations=[rec]), key="0_1")


def test_checklist_updates_badge_and_survives_rerender():
    at = AppTest.from_function(_script).run()
    assert [c.label for c in at.checkbox] == ["**소득** — 중위소득 60% 이하", "**주택 조건** — 보증금 5천만원 이하"]
    head = lambda: next(m.value for m in at.markdown if "card-head" in m.value)  # noqa: E731
    assert "확인 필요 2건" in head()

    at.checkbox[0].check().run()
    assert "2건 중 1건 체크" in head()
    at.checkbox[1].check().run()
    assert "체크한 조건 모두 충족" in head()
    assert at.session_state.checks == {"chk_0_1_1_0": True, "chk_0_1_1_1": True}   # 위젯과 따로 저장 (주제 이동 대비)

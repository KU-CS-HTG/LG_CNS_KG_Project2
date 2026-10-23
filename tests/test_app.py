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


def test_scope_marks_local_and_national_agencies_only_when_sure():
    assert "📍 서울 동대문구" in app._scope_html("서울특별시 동대문구")
    assert "📍 충북 충주시" in app._scope_html("충청북도 충주시")
    assert "📍 서울 지역" in app._scope_html("서울특별시")
    assert "전국" in app._scope_html("보건복지부") and "전국" in app._scope_html("한국장학재단")
    assert "전국" in app._scope_html("국세청")
    assert app._scope_html("재단법인용산복지재단") == ""          # 이름만으로 지역을 알 수 없으면 표시하지 않는다
    assert app._scope_html("(재)인천인재평생교육진흥원") == ""


def test_days_left_only_for_open_applications_closing_soon():
    from datetime import date
    today = date(2026, 10, 22)
    assert app._days_left("2026. 10. 1. ~ 2026. 10. 27.", today) == 5
    assert app._days_left("2026년 10월 22일까지", today) == 0
    assert app._days_left("2026. 10. 1. ~ 2026. 12. 31.", today) is None      # 아직 넉넉함
    assert app._days_left("2026. 10. 25. ~ 2026. 10. 30.", today) is None     # 아직 접수 전
    assert app._days_left("상시 신청", today) is None
    assert app._days_left("2026. 10. 1. ~ 예산 소진 시까지", today) is None   # 끝이 열린 기간
    assert "D-5" in app._deadline_html("2026. 10. 1. ~ 2026. 10. 27.", today)
    assert "D-day" in app._deadline_html("2026년 10월 22일까지", today)
    assert "마감" not in app._deadline_html("상시 신청", today)


def test_ask_reports_progress_steps_in_order(monkeypatch):
    from src.common.schema import Answer
    from src.generate import generator

    def fake_answer(question):
        for step in (generator.STEP_CONDITIONS, generator.STEP_SEARCH, generator.STEP_GENERATE):
            generator._progress(step)
        return Answer(text="", no_evidence=True)
    monkeypatch.setattr(app, "_search_ready", lambda: False)
    monkeypatch.setattr(generator, "answer_question", fake_answer)
    steps = []
    app._ask("질문", steps.append)
    assert steps == [generator.STEP_CONDITIONS, generator.STEP_SEARCH, generator.STEP_GENERATE]
    assert set(steps) <= set(app.STEP_LABELS)
    assert generator.PROGRESS.get() is None                       # 끝나면 알림을 풀어 다음 호출에 남지 않는다
    generator._progress(generator.STEP_SEARCH)                     # 알림이 없으면 아무 일도 없다


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

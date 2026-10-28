"""이어진 질문의 판정 문서 자동 연결: 운영 세부도 판정 문서로, 관련 조항이 없으면 다시 추천하지 않고 '찾을 수 없어요'
(app/main.py, guide_router, guide_evidence).

LLM·검색 모델은 부르지 않는다: guide_router.is_guide_question과 판정 문서 검색을 가짜로 바꿔 끼운다.
"""
from __future__ import annotations

import sys
import types

import pytest

from src.common.schema import GuideChunk
from src.generate import guide_evidence, guide_router

TEXT_END = ("취업지원 종료\n"
            "주 30시간 미만 또는 월 소득 250만원 미만 불완전 취업자는 취업지원 종료 대상에 해당하지 않고 계속하여 참여가 가능")


@pytest.fixture
def guides(monkeypatch):
    state = types.SimpleNamespace(results=[], calls=[])

    def search_guides(query, service_ids=None, region=None, k=3):
        state.calls.append(query)
        return list(state.results)
    module = types.ModuleType("src.index.guide_store")
    module.search_guides = search_guides
    monkeypatch.setitem(sys.modules, "src.index.guide_store", module)
    return state


def _main_script():
    import streamlit as st

    from app import main as app
    from src.common.schema import Answer, Recommendation

    app._search_ready = lambda: False
    app._ask = lambda question, on_step=None: Answer(text="다시 추천: " + question, recommendations=[])
    if "threads" not in st.session_state:
        rec = Recommendation(service_name="국민취업지원제도", agency="고용노동부", reason="이유", benefit="수당",
                             how_to_apply="방문", needs_check=["소득 — 기준 중위소득 60% 이하"], service_id="149200005007")
        st.session_state.threads = [[{"role": "user", "content": "취업 준비 중인 27살이에요", "follow": False},
                                     {"role": "assistant", "content": Answer(text="", recommendations=[rec])}]]
        st.session_state.current = 0
    app.main()


def _ask_follow_up(question: str):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_main_script).run(timeout=30)
    [follow] = [b for b in at.button if "이어서 질문하기" in b.label]
    follow.click().run(timeout=30)
    at.chat_input[0].set_value(question).run(timeout=30)
    return at


def _end_clause():
    return GuideChunk(chunk_id="emp#p114", doc_id="emp", doc_title="국민취업지원제도 업무매뉴얼", publisher="고용노동부",
                      year=2026, page=114, heading="취업지원 종료", text=TEXT_END, scope="전국", service_ids=[],
                      source_url="")


def test_auto_routed_question_without_clause_says_not_found_instead_of_recommending(guides, monkeypatch):
    pytest.importorskip("streamlit")
    monkeypatch.setattr(guide_router, "is_guide_question", lambda q, recs: True)
    at = _ask_follow_up("중간에 일을 구하면 생계급여가 끊겨요?")
    thread = at.session_state.threads[0]
    assert guides.calls                                                   # 판정 문서를 찾았다
    assert thread[-2]["guide"] is True and thread[-1]["guide"] is True     # 다시 추천하지 않았다
    assert thread[-1]["content"].text == guide_evidence.NO_GUIDE_ANSWER and not thread[-1]["content"].citations
    assert not at.exception


def test_clause_that_does_not_answer_is_not_shown(guides, monkeypatch):
    """조항을 찾았어도 LLM이 읽고 질문에 답하지 않는다고 하면(answerable=false) 엉뚱한 원문 대신 '찾을 수 없어요'."""
    from src.common.schema import Recommendation
    from src.generate import condition_extractor
    guides.results = [_end_clause()]
    calls = []
    monkeypatch.setattr(condition_extractor, "_chat_json",
                        lambda *a: calls.append(a) or {"evidence": [], "answerable": False})
    rec = Recommendation(service_name="생계급여", agency="보건복지부", reason="", benefit="", how_to_apply="",
                         service_id="WII000001410")
    ans = guide_evidence.answer_guide_question("불완전 취업자는 취업지원 종료돼요?", [rec], interpret=True)
    assert len(calls) == 1                                                # 조항을 찾아 LLM이 읽었다 (다시 쓰지 않음)
    assert ans.no_evidence and ans.text == guide_evidence.NO_GUIDE_ANSWER and not ans.citations


def test_search_question_still_recommends(guides, monkeypatch):
    """'비슷한 제도 추천해줘'처럼 search로 간 질문은 지금처럼 다시 추천한다."""
    pytest.importorskip("streamlit")
    monkeypatch.setattr(guide_router, "is_guide_question", lambda q, recs: False)
    at = _ask_follow_up("이것과 비슷한 제도를 몇 개 추천해줘")
    thread = at.session_state.threads[0]
    assert thread[-1]["content"].text.startswith("다시 추천: ") and not guides.calls


def test_auto_routed_question_with_clause_answers_from_guide(guides, monkeypatch):
    pytest.importorskip("streamlit")
    monkeypatch.setattr(guide_router, "is_guide_question", lambda q, recs: True)
    monkeypatch.setattr("src.generate.guide_answer.explain", lambda *a, **k: None)
    guides.results = [_end_clause()]
    at = _ask_follow_up("중간에 취업하면 어떻게 돼요?")
    thread = at.session_state.threads[0]
    assert thread[-2]["guide"] is True and thread[-1]["guide"] is True
    assert [c.page for c in thread[-1]["content"].citations] == [114]


@pytest.mark.parametrize("question, term", [
    ("중간에 취업하면 어떻게 돼요?", "종료"),
    ("중간에 일을 구하면 생계급여가 끊겨요?", "종료"),
    ("제가 버는 돈이 많으면 부모님이랑 가구를 나눌 수 있나요?", "소득"),
    ("제가 버는 돈이 많으면 부모님이랑 가구를 나눌 수 있나요?", "가구분리"),
])
def test_question_words_bridge_to_document_words(question, term):
    assert term in guide_evidence._doc_terms(question)


def test_router_prompt_sends_program_details_to_guide_and_card_facts_to_search():
    """운영 세부(특례 신청 장소·기간·취업 시 처리)는 guide, 카드에 있는 일반 신청 방법·기한·금액은 search."""
    guide_part, search_part = guide_router.PROMPT.split("- search:")
    for words in ("어디에·어떻게 신청", "얼마 동안", "중간에 취업"):
        assert words in guide_part
    for words in ("일반 신청 방법", "신청 기한", "받는 금액"):
        assert words in search_part

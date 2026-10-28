"""판정 기준 문서 조항으로 이어진 질문에 답 쓰기 (src/generate/guide_answer.py, guide_evidence.answer_guide_question).

LLM은 부르지 않는다: condition_extractor._chat_json을 가짜로 바꿔 끼우고, 판정 문서 검색은 test_generate_guides의
fake_guides와 같은 방식으로 sys.modules에 가짜 모듈을 넣는다.
"""
from __future__ import annotations

import sys
import types

import pytest

from src.common.schema import GuideChunk, Recommendation
from src.generate import guide_answer, guide_evidence

TEXT_FAMILY = ("가구원의 범위\n"
               "개별가구는 보장을 받는 단위로, 수급권자를 포함하여 동일한 주민등록표상에 등재된 사람으로 한다.\n"
               "다만 주민등록상 따로 살더라도 생계·주거를 같이하는 부모는 가구원에 포함할 수 있다.\n"
               "소득인정액은 소득평가액과 재산의 소득환산액을 합한 금액이다.")
QUESTION = "부모님과 따로 살면 같은 가구예요?"
RECS = [Recommendation("주거급여", "국토교통부", "", "", "", service_id="SVC2")]
GOOD = ("주민등록이 달라도 생계와 주거를 같이하는 부모님이면 가구원에 포함될 수 있어요 [1]. "
        "기본은 같은 주민등록표에 올라 있는 사람을 한 가구로 봐요 [1].")
EVIDENCE = [{"n": 1, "quote": "주민등록상 따로 살더라도 생계·주거를 같이하는 부모는 가구원에 포함할 수 있다."}]


def reply(answer=GOOD, evidence=EVIDENCE, answerable=True):
    return {"evidence": evidence, "answerable": answerable, "rule": answer, "exception": "", "situation": ""}


@pytest.fixture
def fake_guides(monkeypatch):
    def search_guides(query, service_ids=None, region=None, k=3):
        return [GuideChunk(chunk_id="basic#p52-1", doc_id="basic2026", doc_title="2026년 국민기초생활보장사업 안내",
                           publisher="보건복지부", year=2026, page=52, heading="가구 기준", text=TEXT_FAMILY,
                           scope="전국", service_ids=[], source_url="https://www.mohw.go.kr/guide.pdf")]
    module = types.ModuleType("src.index.guide_store")
    module.search_guides = search_guides
    monkeypatch.setitem(sys.modules, "src.index.guide_store", module)


@pytest.fixture
def fake_llm(monkeypatch):
    state = types.SimpleNamespace(reply=reply(), calls=[])

    def chat_json(system, user):
        state.calls.append(user)
        if isinstance(state.reply, Exception):
            raise state.reply
        return state.reply
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", chat_json)
    return state


def test_interpreted_answer_uses_verified_clause_text(fake_guides, fake_llm):
    ans = guide_evidence.answer_guide_question(QUESTION, RECS, interpret=True, context="부모님과 따로 사는 25살")
    assert ans.interpreted and ans.text == GOOD and [c.page for c in ans.citations] == [52]
    [user] = fake_llm.calls
    assert "주거급여" in user and QUESTION in user and "앞서 한 말: 부모님과 따로 사는 25살" in user
    assert "[1] (2026년 국민기초생활보장사업 안내, 52쪽)" in user
    assert "소득인정액은" in user                     # 화면 인용문보다 넓은 조항 본문을 읽고 쓴다


def test_default_and_eval_do_not_call_llm(fake_guides, fake_llm):
    ans = guide_evidence.answer_guide_question(QUESTION, RECS)
    assert not ans.interpreted and ans.text == guide_evidence.FOUND_GUIDE_ANSWER.format(n=1)
    assert fake_llm.calls == []


@pytest.mark.parametrize("bad", [
    reply(answer="주민등록이 달라도 가구원에 포함될 수 있어요.", evidence=[]),            # 근거 번호·구절 없음
    reply(answer="가구원에 포함될 수 있어요 [3]."),                                  # 없는 번호
    reply(answer="따로 살아도 같은 가구의 대상입니다 [1]."),                           # 자격 단정 (낮출 말이 없음)
    reply(answer="주거급여를 받을 자격이 있어요 [1]."),
    reply(answer="가 [1]" * 400),                                                 # 조항을 통째로 옮겨 적음
    reply(evidence=[]),                                                          # 근거 구절 없음
    reply(evidence=[{"n": 1, "quote": "부모와 따로 살면 같은 가구로 볼 수 없다"}]),       # 조항에 없는 구절 (결론 뒤집기)
    reply(evidence=[{"n": 1, "quote": "가구원"}]),                                 # 너무 짧아 대조가 안 됨
    reply(evidence=[{"n": 1, "quote": ""}, "not a dict"]),
    ["not", "a", "dict"],
    RuntimeError("network"),                                                     # LLM 실패
])
def test_bad_or_failed_answer_falls_back_to_fixed_text(fake_guides, fake_llm, bad):
    fake_llm.reply = bad
    ans = guide_evidence.answer_guide_question(QUESTION, RECS, interpret=True)
    assert not ans.interpreted and ans.text == guide_evidence.FOUND_GUIDE_ANSWER.format(n=1)
    assert [c.page for c in ans.citations] == [52]                               # 원문 근거는 그대로 보인다


def test_no_clause_means_no_llm_call(fake_llm, monkeypatch):
    module = types.ModuleType("src.index.guide_store")
    module.search_guides = lambda *a, **k: []
    monkeypatch.setitem(sys.modules, "src.index.guide_store", module)
    ans = guide_evidence.answer_guide_question(QUESTION, RECS, interpret=True)
    assert ans.no_evidence and ans.text == guide_evidence.NO_GUIDE_ANSWER and fake_llm.calls == []


def test_assertive_wording_in_rule_description_is_softened():
    """'Ⅱ유형은 취업경험이 없어도 지원받을 수 있어요': 맞는 기준 설명인데 말투 때문에 두 번 다 걸렸다 -> 말투만 낮춘다."""
    soft = guide_answer.checked(reply("Ⅱ유형은 취업경험이 없어도 지원받을 수 있어요 [1]. 같은 가구에 해당됩니다 [1]."),
                                [TEXT_FAMILY])
    assert soft == "Ⅱ유형은 취업경험이 없어도 지원받을 가능성이 있어요 [1]. 같은 가구에 해당될 수 있어요 [1]."
    assert guide_answer.checked(reply("미취업 청년은 참여할 수 있어요 [1]."), [TEXT_FAMILY]).endswith("참여할 가능성이 있어요 [1].")
    assert guide_answer.checked(reply("주민센터에서 신청할 수 있어요 [1]."), [TEXT_FAMILY])   # 절차 설명은 그대로
    from eval import answer_eval
    assert not answer_eval._ASSERTIVE_RE.search(soft)                                     # 평가의 단정 표현에도 안 걸린다


def test_search_adds_program_name_in_second_search_and_interleaves(monkeypatch):
    """'일한 경험이 없어도 참여할 수 있나요?': 이름 없이 찾으면 일경험(인턴) 사업 쪽만, 이름을 붙이면 참여 요건 쪽이 나왔다."""
    calls = []

    def search_guides(query, service_ids=None, region=None, k=3):
        calls.append(query)
        page = 10 if query.startswith("국민취업지원제도 ") else 174
        return [GuideChunk(chunk_id=f"emp#p{page}", doc_id="emp", doc_title="국민취업지원제도 업무매뉴얼", publisher="고용노동부",
                           year=2026, page=page, heading="", text=f"참여 요건\n최근 2년 이내 취업경험 100일 이상 ({page}쪽)",
                           scope="전국", service_ids=[], source_url="")]
    module = types.ModuleType("src.index.guide_store")
    module.search_guides = search_guides
    monkeypatch.setitem(sys.modules, "src.index.guide_store", module)
    recs = [Recommendation("국민취업지원제도", "고용노동부", "", "", "", service_id="149200005007")]
    ans = guide_evidence.answer_guide_question("일한 경험이 없어도 참여할 수 있나요?", recs)
    assert calls == ["일한 경험이 없어도 참여할 수 있나요? 취업경험 요건 지원대상",
                     "국민취업지원제도 일한 경험이 없어도 참여할 수 있나요? 취업경험 요건 지원대상"]
    assert [c.page for c in ans.citations] == [174, 10]
    assert guide_evidence._program_name("주거급여 (맞춤형 급여)") == "주거급여"
    assert "요건" not in guide_evidence._keywords("일한 경험이 없어도 참여할 수 있나요?")   # 검색에만 쓰는 말


def test_checked_needs_grounded_quote_for_every_cited_clause():
    clauses = [TEXT_FAMILY, "다만, 30세 미만의 미혼자녀가 부모와\n주거를 달리하면서 다음 ①∼④의 경우에 해당하여"]
    text = "기본은 같은 주민등록표 기준이에요 [1]. 다만 예외가 있어요 [2]."
    both = [EVIDENCE[0], {"n": 2, "quote": "30세 미만의 미혼자녀가 부모와 주거를 달리하면서"}]   # 줄바꿈은 무시하고 대조
    assert guide_answer.checked(reply(text, both), clauses) == text
    assert guide_answer.checked(reply(text, EVIDENCE), clauses) is None                     # [2]의 근거 구절 없음
    assert guide_answer.checked(reply(text, both), clauses[:1]) is None                      # 없는 번호


def test_quote_is_matched_to_the_clause_that_contains_it_not_the_number_llm_wrote():
    """gpt-4o-mini가 구절은 정확히 옮기고 번호만 조항 안의 문서 번호('4-83')와 헷갈려 4로 적었다."""
    clauses = ["4-83\n" + TEXT_FAMILY, "제3장 조사 절차"]
    assert guide_answer.checked(reply(GOOD, [{"n": 4, "quote": EVIDENCE[0]["quote"]}]), clauses) == GOOD
    assert guide_answer.checked(reply("조사를 마쳐요 [2].", [{"n": 2, "quote": EVIDENCE[0]["quote"]}]), clauses) is None


def test_parts_joined_rule_exception_situation_and_every_sentence_cited():
    data = {"evidence": EVIDENCE, "answerable": True, "situation": "앞서 말한 처지는 생계를 같이하는지 확인해 봐요 [1].",
            "rule": "같은 주민등록표에 있는 사람이 한 가구예요. [1]", "exception": ""}
    assert guide_answer.checked(data, [TEXT_FAMILY]) == (
        "같은 주민등록표에 있는 사람이 한 가구예요 [1]. 앞서 말한 처지는 생계를 같이하는지 확인해 봐요 [1].")
    data["situation"] = "그러니 주민센터에 물어보세요."                    # 근거 번호 없는 처지 문장은 그 문장만 뺀다
    assert guide_answer.checked(data, [TEXT_FAMILY]) == "같은 주민등록표에 있는 사람이 한 가구예요 [1]."
    data["exception"] = "다만 따로 살면 달라요."           # 근거 구절이 조항 하나에서만 나왔으면 그 번호를 단다
    assert guide_answer.checked(data, [TEXT_FAMILY]).endswith("다만 따로 살면 달라요 [1].")
    two = [TEXT_FAMILY, "다만 30세 미만 미혼자녀는 분리하여 보장할 수 있다"]
    data["evidence"] = EVIDENCE + [{"n": 2, "quote": "30세 미만 미혼자녀는 분리하여 보장"}]
    data["rule"] = "같은 주민등록표에 있는 사람이 한 가구예요 [1]. 다만 예외가 있어요 [2]."
    assert guide_answer.checked(data, two) is None                 # 두 조항에서 나왔으면 어느 쪽인지 몰라 받지 않는다


def test_rejected_answer_is_rewritten_once_with_the_reason(fake_guides, fake_llm, monkeypatch):
    replies = [reply(answer="Ⅱ유형은 취업경험이 없어도 지원 대상입니다 [1]."), reply()]
    monkeypatch.setattr(fake_llm, "reply", None)
    monkeypatch.setattr("src.generate.condition_extractor._chat_json",
                        lambda system, user: (fake_llm.calls.append(user), replies.pop(0))[1])
    ans = guide_evidence.answer_guide_question(QUESTION, RECS, interpret=True)
    assert ans.interpreted and ans.text == GOOD
    first, second = fake_llm.calls
    assert "쓸 수 없었다" not in first and "'대상입니다'처럼 자격을 단정했다" in second


def test_rewrite_happens_only_once(fake_guides, fake_llm):
    fake_llm.reply = reply(answer="같은 가구의 대상입니다 [1].")
    ans = guide_evidence.answer_guide_question(QUESTION, RECS, interpret=True)
    assert not ans.interpreted and len(fake_llm.calls) == 2
    fake_llm.reply, fake_llm.calls[:] = reply(answer="", answerable=False), []
    guide_evidence.answer_guide_question(QUESTION, RECS, interpret=True)
    assert len(fake_llm.calls) == 1                                # 조항으로 답할 수 없다고 하면 다시 쓰지 않는다


def _main_script():
    import streamlit as st

    from app import main as app
    from src.common.schema import Answer, Recommendation

    app._search_ready = lambda: False
    if "threads" not in st.session_state:
        rec = Recommendation(service_name="주거급여", agency="국토교통부", reason="이유", benefit="임차료",
                             how_to_apply="방문", needs_check=["가구 — 동일한 주민등록표상에 등재된 사람"], service_id="SVC2")
        st.session_state.threads = [[{"role": "user", "content": "서울에 사는 25살인데 주거급여 받을 수 있나요?",
                                      "follow": False},
                                     {"role": "assistant", "content": Answer(text="", recommendations=[rec])}]]
        st.session_state.current = 0
    app.main()


def test_app_shows_answer_then_collapsed_numbered_sources(fake_guides, fake_llm):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_main_script).run(timeout=30)
    [ask_guide] = [b for b in at.button if "판정 기준으로 물어보기" in b.label]
    ask_guide.click().run(timeout=30)
    at.chat_input[0].set_value(QUESTION).run(timeout=30)
    assert at.session_state.threads[0][-1]["content"].interpreted
    assert any("가구원에 포함될 수 있어요 [1]" in m.value for m in at.markdown)
    [box] = [e for e in at.expander if "판정 기준 문서 원문 보기" in e.label]
    assert not getattr(box.proto, "expanded", False)
    assert any("guide-box" in m.value and "[1] " in m.value and "52쪽" in m.value for m in box.markdown)

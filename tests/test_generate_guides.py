"""C: 판정 기준 문서 근거(src/generate/guide_evidence.py) 테스트.

B의 src/index/guide_store.py가 아직 없어도 돌도록 가짜 search_guides를 이 파일 안에서 끼운다
(conftest.py는 건드리지 않음). guide_evidence는 importlib로 'src.index.guide_store'를 찾으므로
sys.modules에 가짜 모듈을 넣으면 실제 모듈이 생긴 뒤에도 그대로 가짜가 쓰인다.
"""
from __future__ import annotations

import sys
import types

import pytest

from src.common import config
from src.common.schema import Citation, GuideChunk
from src.generate import generator, guide_evidence

BASIC = dict(doc_id="basic2026", doc_title="2026년 국민기초생활보장사업 안내", publisher="보건복지부", year=2026,
             source_url="https://www.mohw.go.kr/guide.pdf")
TEXT_FAMILY = ("가구원의 범위\n"
               "개별가구는 보장을 받는 단위로, 수급권자를 포함하여 동일한 주민등록표상에 등재된 사람으로 한다.\n"
               "다만 주민등록상 따로 살더라도 생계·주거를 같이하는 부모는 가구원에 포함할 수 있다.\n"
               "소득인정액은 소득평가액과 재산의 소득환산액을 합한 금액이다.")
TEXT_UNRELATED = "제3장 조사 절차\n담당 공무원은 신청 접수 후 14일 이내에 조사를 마친다."


def gchunk(chunk_id: str, page: int, text: str, **kw) -> GuideChunk:
    fields = {**BASIC, "heading": f"{BASIC['doc_title']} > 가구 기준", **kw}
    return GuideChunk(chunk_id=chunk_id, page=page, text=text, **fields)


@pytest.fixture
def fake_guides(monkeypatch):
    """가짜 search_guides. results를 바꿔 돌려줄 조각을 정하고, calls로 호출 인자를 확인한다."""
    state = types.SimpleNamespace(results=[], calls=[], error=None)

    def search_guides(query, service_ids=None, region=None, k=3):
        state.calls.append({"query": query, "service_ids": service_ids, "region": region, "k": k})
        if state.error:
            raise state.error
        return list(state.results)
    module = types.ModuleType("src.index.guide_store")
    module.search_guides = search_guides
    monkeypatch.setitem(sys.modules, "src.index.guide_store", module)
    return state


ITEMS = ["가구 — 동일한 주민등록표상에 등재된 사람", "소득 — 기준 중위소득 32% 이하"]


def test_query_drops_code_notes_and_generic_item():
    q = guide_evidence.guide_query("생계급여", ["가구원 수 (기준은 원문 확인)", "거주 지역 조건 (소관기관: 서울특별시, 원문 확인)",
                                            "자격 요건 충족 여부", "소득 — 기준 중위소득 32% 이하"])
    assert q == "생계급여 가구원 수 거주 지역 조건 소득 기준 중위소득 32% 이하"


def test_one_search_per_card_with_service_and_region(fake_guides):
    fake_guides.results = [gchunk("basic#p52-1", 52, TEXT_FAMILY)]
    cits = guide_evidence.find_guides("SVC1", "생계급여", ITEMS, region="서울")
    assert len(fake_guides.calls) == 1                                     # 항목이 여러 개여도 카드당 1번
    assert fake_guides.calls[0]["service_ids"] == ["SVC1"] and fake_guides.calls[0]["region"] == "서울"
    assert fake_guides.calls[0]["k"] == guide_evidence.GUIDE_K
    [c] = cits
    assert isinstance(c, Citation) and c.kind == "guide" and c.page == 52
    assert c.service_name == "2026년 국민기초생활보장사업 안내" and c.agency == "보건복지부"
    assert c.detail_url == BASIC["source_url"]


def test_quote_is_contiguous_original_text_within_limit(fake_guides):
    long_text = TEXT_FAMILY + "\n" + "\n".join(f"부칙 {i}. 이 지침은 공포한 날부터 시행한다." for i in range(40))
    fake_guides.results = [gchunk("basic#p52-1", 52, long_text)]
    [c] = guide_evidence.find_guides("SVC1", "생계급여", ITEMS)
    assert c.quote in long_text                                            # 요약·바꿔 쓰기 없이 원문 일부
    assert len(c.quote) <= guide_evidence.MAX_GUIDE_QUOTE_CHARS
    assert "주민등록표상에 등재된" in c.quote                                # 핵심 낱말이 많은 줄이 들어 있다


def test_other_region_unrelated_and_duplicate_chunks_are_dropped(fake_guides):
    fake_guides.results = [
        gchunk("seoul#p3-1", 3, TEXT_FAMILY, doc_id="seoul", scope="부산광역시"),   # 다른 지역 문서
        gchunk("basic#p60-1", 60, TEXT_UNRELATED),                                # 검색은 됐지만 관련 없음
        gchunk("basic#p52-1", 52, TEXT_FAMILY),
        gchunk("basic#p52-2", 52, TEXT_FAMILY),                                   # 같은 문서·같은 쪽
    ]
    cits = guide_evidence.find_guides("SVC1", "생계급여", ITEMS, region="서울")
    assert [(c.page, c.service_name) for c in cits] == [(52, "2026년 국민기초생활보장사업 안내")]


def test_regional_document_only_for_matching_region(fake_guides):
    fake_guides.results = [gchunk("seoul#p3-1", 3, TEXT_FAMILY, doc_id="seoul", scope="서울특별시")]
    assert len(guide_evidence.find_guides("SVC1", "생계급여", ITEMS, region="서울")) == 1   # 표기가 달라도 같은 지역
    assert guide_evidence.find_guides("SVC1", "생계급여", ITEMS, region="대전") == []
    assert guide_evidence.find_guides("SVC1", "생계급여", ITEMS) == []                    # 지역을 모르면 지역 문서는 안 씀


def test_draft_and_year_labels(fake_guides):
    fake_guides.results = [gchunk("h#p1-1", 1, "행정예고: 가구원의 범위를 동일한 주민등록표 기준으로 개정",
                                  doc_title="주거급여 선정기준", publisher="국토교통부", year=2027)]
    [c] = guide_evidence.find_guides("SVC2", "주거급여", ["가구 — 동일한 주민등록표"])
    assert c.service_name == f"주거급여 선정기준 (2027년) · {guide_evidence.DRAFT_MARK}"


def test_missing_or_failing_guide_store_returns_empty(fake_guides, monkeypatch):
    fake_guides.error = RuntimeError("색인 없음")
    assert guide_evidence.find_guides("SVC1", "생계급여", ITEMS) == []
    monkeypatch.setitem(sys.modules, "src.index.guide_store", None)       # import 실패 (B 미구현)
    assert guide_evidence.find_guides("SVC1", "생계급여", ITEMS) == []


def test_no_checkable_items_means_no_search(fake_guides):
    assert guide_evidence.find_guides("SVC1", "생계급여", ["자격 요건 충족 여부"]) == []
    assert fake_guides.calls == []


def test_question_region():
    assert guide_evidence.question_region("서울에 사는 27살인데 부모님과 따로 살아요") == "서울"
    assert guide_evidence.question_region("부모님과 따로 살면 같은 가구예요?") is None


def test_available_follows_guide_store_module(fake_guides, monkeypatch):
    assert guide_evidence.available() is True
    monkeypatch.setitem(sys.modules, "src.index.guide_store", None)
    assert guide_evidence.available() is False


def _card_script():
    import streamlit as st

    from app import main as app
    from src.common.schema import Answer, Recommendation

    st.session_state.setdefault("checks", {})
    st.session_state.setdefault("guides", {})
    rec = Recommendation(service_name="생계급여", agency="보건복지부", reason="이유", benefit="생계비", how_to_apply="방문",
                         needs_check=["가구 — 동일한 주민등록표상에 등재된 사람"], service_id="SVC1")
    app._render_answer(Answer(text="", recommendations=[rec]), key="0_1", region="서울")


def test_card_button_searches_once_and_shows_guide(fake_guides):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    fake_guides.results = [gchunk("basic#p52-1", 52, TEXT_FAMILY)]
    at = AppTest.from_function(_card_script).run()
    assert fake_guides.calls == []                                         # 답변을 그릴 때는 검색하지 않는다
    [button] = [b for b in at.button if "판정 기준" in b.label]
    button.click().run()
    box = lambda: next((m.value for m in at.markdown if "guide-box" in m.value), "")  # noqa: E731
    assert "52쪽" in box() and "주민등록표상에 등재된" in box() and "보조금24" in box()
    assert len(fake_guides.calls) == 1 and fake_guides.calls[0]["region"] == "서울"
    at.run()                                                               # 다시 그려도
    assert len(fake_guides.calls) == 1 and "52쪽" in box()                 # 재검색 없이 저장된 결과
    assert not [b for b in at.button if "판정 기준" in b.label]            # 버튼은 결과로 바뀐다


def test_card_hides_guide_button_without_guide_store(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    monkeypatch.setitem(sys.modules, "src.index.guide_store", None)       # B 미구현
    at = AppTest.from_function(_card_script).run()
    assert not [b for b in at.button if "판정 기준" in b.label]


def test_follow_up_keywords_drop_question_endings(monkeypatch):
    monkeypatch.setattr(guide_evidence, "_kiwi_nouns", lambda text: None)   # Kiwi가 없는 PC (정규식 방식)
    assert guide_evidence._keywords("부모님과 따로 살면 같은 가구예요?") == ["부모님", "따로", "가구"]


def test_kiwi_keywords_keep_nouns_only():
    pytest.importorskip("kiwipiepy")                                        # requirements.txt 밖 (설치된 PC에서만)
    assert guide_evidence._keywords("부모님과 따로 사는 25살인데 주거급여를 따로 받을 수 있나요?") == ["부모", "주거급여"]
    assert guide_evidence._keywords("자동차가 있으면 재산으로 계산되나요?") == ["자동차", "재산", "계산"]


def test_guide_question_answers_with_original_clauses_only(fake_guides):
    from src.common.schema import Recommendation
    recs = [Recommendation("생계급여", "보건복지부", "", "", "", service_id="SVC1"),
            Recommendation("주거급여", "국토교통부", "", "", "", service_id="SVC2"),
            Recommendation("이름만", "기관", "", "", "")]                     # service_id 없는 카드는 빼고 찾는다
    fake_guides.results = [gchunk("basic#p52-1", 52, TEXT_FAMILY), gchunk("basic#p60-1", 60, TEXT_UNRELATED)]
    ans = guide_evidence.answer_guide_question("부모님과 따로 살면 같은 가구예요?", recs, region="서울")
    assert fake_guides.calls == [{"query": "부모님과 따로 살면 같은 가구예요?", "service_ids": ["SVC1", "SVC2"],
                                  "region": "서울", "k": guide_evidence.GUIDE_K}]
    assert not ans.no_evidence and [c.page for c in ans.citations] == [52]       # 관련 없는 조항은 뺀다
    assert "따로 살더라도" in ans.citations[0].quote
    assert ans.text == guide_evidence.FOUND_GUIDE_ANSWER.format(n=1)
    assert "해당됩니다" not in ans.text and "받을 수 있" not in ans.text          # 자격을 단정하지 않는다

    fake_guides.results = [gchunk("basic#p60-1", 60, TEXT_UNRELATED)]
    none = guide_evidence.answer_guide_question("부모님과 따로 살면 같은 가구예요?", recs)
    assert none.no_evidence and none.citations == [] and none.text == guide_evidence.NO_GUIDE_ANSWER


def _main_script():
    import streamlit as st

    from app import main as app
    from src.common.schema import Answer, Recommendation

    app._search_ready = lambda: False                                      # 실제 검색 모델을 불러오지 않게
    if "threads" not in st.session_state:
        rec = Recommendation(service_name="생계급여", agency="보건복지부", reason="이유", benefit="생계비",
                             how_to_apply="방문", needs_check=["가구 — 동일한 주민등록표상에 등재된 사람"], service_id="SVC1")
        st.session_state.threads = [[{"role": "user", "content": "서울에 사는데 생계가 어려워요", "follow": False},
                                     {"role": "assistant", "content": Answer(text="", recommendations=[rec])}]]
        st.session_state.current = 0
    app.main()


def test_follow_up_guide_question_flow(fake_guides):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    fake_guides.results = [gchunk("basic#p52-1", 52, TEXT_FAMILY)]
    at = AppTest.from_function(_main_script).run(timeout=30)
    [ask_guide] = [b for b in at.button if "판정 기준으로 물어보기" in b.label]
    ask_guide.click().run(timeout=30)
    assert "판정 기준" in at.chat_input[0].placeholder
    at.chat_input[0].set_value("부모님과 따로 살면 같은 가구예요?").run(timeout=30)
    thread = at.session_state.threads[0]
    assert thread[-2] == {"role": "user", "content": "부모님과 따로 살면 같은 가구예요?", "follow": True, "guide": True}
    assert thread[-1]["guide"] is True and thread[-1]["content"].citations[0].page == 52
    assert fake_guides.calls[-1]["service_ids"] == ["SVC1"] and fake_guides.calls[-1]["region"] == "서울"
    assert any("guide-box" in m.value and "52쪽" in m.value for m in at.markdown)
    assert at.session_state.follow is False                                # 다음 입력은 다시 새 질문


def test_merged_question_skips_guide_questions():
    from app import main as app
    thread = [{"role": "user", "content": "서울 사는 27살"}, {"role": "assistant", "content": None},
              {"role": "user", "content": "부모님과 따로 살면 같은 가구예요?", "guide": True},
              {"role": "assistant", "content": None, "guide": True}]
    assert app._merged_question(thread, "소득은 월 150만 원") == "서울 사는 27살 소득은 월 150만 원"


def test_recommendation_carries_service_id(sample_chunks, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "none.db")         # 신청기한 원문 조회는 하지 않게
    chunks = [c for c in sample_chunks if c.service_id == "SAMPLE001"]
    rec = generator._to_recommendation("월세 지원 있나요?", chunks, {})
    assert rec.service_id == "SAMPLE001"

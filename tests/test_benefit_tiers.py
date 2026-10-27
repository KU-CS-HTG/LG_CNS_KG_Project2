"""조건별 지원 정리(benefit_tiers)와 카드의 '내 조건으로 받을 수 있는 지원'. LLM은 가짜로 바꿔 끼운다."""
import pytest

from src.common.schema import Chunk
from src.generate import benefit_tiers as bt
from src.generate import condition_extractor as ce

SUPPORT = ("○ 월 15회 이상 대중교통 이용 시 지출액의 일정비율 환급\n - 일반층 : 20%\n - 청년층 : 30%\n  ㆍ청년기본법에 따른 만19~34세\n"
           " - 저소득층 : 53%\n - 3자녀 이상 가구 : 50%")
TARGET = "○ 만 19세 이상의 국민\n○ 청년층 : 만19~34세"
NEEDS = ["연령 — 만 19세 이상", "연령 청년 — 만19~34세", "소득 — 기초생활수급자 및 차상위계층", "자녀 — 3자녀 이상 가구"]


def chunks():
    mk = lambda sec, text: Chunk(f"S1#{sec}", "S1", "K-패스", sec, f"K-패스\n{text}")
    return [mk("지원내용", SUPPORT), mk("지원대상", TARGET)]


RAW = {"tiers": [
    {"label": "일반", "requires": [1], "benefit": "대중교통비의 20% 환급", "quote": "일반층 : 20%", "basis": "source"},
    {"label": "청년", "requires": [1, 2], "benefit": "대중교통비의 30% 환급", "quote": "청년층 : 30%", "basis": "source"},
    {"label": "저소득", "requires": [3], "benefit": "대중교통비의 53% 환급", "quote": "저소득층 : 53%", "basis": "source"},
    {"label": "다자녀", "requires": [4, 9, True], "benefit": "대중교통비의 90% 환급", "quote": "3자녀 이상 가구 : 90%", "basis": "source"},   # 원문에 없는 숫자
    {"label": "지급 방식", "requires": [], "benefit": "카드사 마일리지로 환급돼요", "quote": "", "basis": "outside"},
    {"label": "", "requires": [], "benefit": "라벨 없음", "quote": "", "basis": "outside"},
], "notes": [{"text": "청년·일반 중 유리한 것 하나만 적용돼요"}, {"text": "  "}]}


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    bt._cache.clear()
    monkeypatch.setenv("WEB_LOOKUP", "0")          # 테스트는 웹 조회를 하지 않는다 (필요한 테스트가 가짜로 바꿔 끼운다)


def test_source_text_orders_sections_and_strips_the_name_prefix():
    text = bt.source_text(chunks())
    assert text.index("[지원대상]") < text.index("[지원내용]") and "K-패스\n" not in text and "일반층 : 20%" in text


def test_clean_downgrades_ungrounded_claims_and_drops_junk():
    out = bt.clean(RAW, bt.source_text(chunks()), len(NEEDS))
    by = {t["label"]: t for t in out["tiers"]}
    assert set(by) == {"일반", "청년", "저소득", "다자녀", "지급 방식"}                    # 라벨 없는 항목 제거
    assert by["청년"]["basis"] == bt.SOURCE and by["청년"]["quote"] == "청년층 : 30%"
    assert by["다자녀"]["basis"] == bt.OUTSIDE and by["다자녀"]["quote"] == ""             # 90은 원문에 없다 -> 자료 밖으로
    assert by["다자녀"]["requires"] == [4]                                                  # 범위 밖 번호·bool 제거
    assert by["지급 방식"]["basis"] == bt.OUTSIDE
    assert out["notes"] == [{"text": "청년·일반 중 유리한 것 하나만 적용돼요", "basis": bt.OUTSIDE}]


def test_quote_must_exist_verbatim_and_be_long_enough():
    raw = {"tiers": [{"label": "a", "requires": [], "benefit": "20% 환급", "quote": "20%", "basis": "source"},
                     {"label": "b", "requires": [], "benefit": "20% 환급", "quote": "일반층 : 20%", "basis": "source"},
                     {"label": "c", "requires": [], "benefit": "20% 환급", "quote": "일반층 : 20 %", "basis": "source"}]}   # 공백만 다르면 인정
    got = {t["label"]: t["basis"] for t in bt.clean(raw, bt.source_text(chunks()), 0)["tiers"]}
    assert got == {"a": bt.OUTSIDE, "b": bt.SOURCE, "c": bt.SOURCE}


def test_evaluate_follows_the_checked_conditions():
    tiers = bt.clean(RAW, bt.source_text(chunks()), len(NEEDS))["tiers"]
    none = bt.evaluate(tiers, set())
    assert [t["label"] for t in none["base"]] == ["지급 방식"] and none["met"] == [] and len(none["pending"]) == 4
    some = bt.evaluate(tiers, {1, 2})
    assert [t["label"] for t in some["met"]] == ["일반", "청년"]
    assert {t["label"]: t["missing"] for t in some["pending"]} == {"저소득": [3], "다자녀": [4]}
    assert [t["label"] for t in bt.evaluate(tiers, {1, 2, 3, 4})["pending"]] == []


def test_compute_calls_llm_once_per_service_and_needs_and_survives_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: calls.append(u) or RAW)
    first = bt.compute("S1", "K-패스", NEEDS, chunks())
    assert first and bt.compute("S1", "K-패스", NEEDS, chunks()) is first and len(calls) == 1      # 두 번째는 기억해 둔 결과
    assert "청년층 : 30%" in calls[0] and "1. 연령" in calls[0]
    bt._cache.clear()
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: (_ for _ in ()).throw(RuntimeError("down")))
    assert bt.compute("S1", "K-패스", NEEDS, chunks()) is None                                      # 실패는 기억하지 않는다
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: {"tiers": []})
    assert bt.compute("S1", "K-패스", NEEDS, chunks()) is None


# --- 카드 화면 ----------------------------------------------------------------------------------------------
def _card_script():
    import streamlit as st
    from app import main as app
    from src.common.schema import Recommendation

    st.session_state.setdefault("checks", {})
    st.session_state.setdefault("guides", {})
    r = Recommendation("K-패스", "국토교통부", "조건 확인", "대중교통비 환급", "온라인", service_id="S1",
                       needs_check=["연령 — 만 19세 이상", "연령 청년 — 만19~34세", "소득 — 기초생활수급자 및 차상위계층", "자녀 — 3자녀 이상 가구"])
    app._card(1, r, "t1")


def _amounts(raw_amounts):
    raw = {"tiers": [{**t, "amount": raw_amounts.get(t["label"], "")} for t in RAW["tiers"]], "notes": []}
    return bt.clean(raw, bt.source_text(chunks()), len(NEEDS))


def test_amount_is_kept_only_when_grounded_in_the_source():
    out = _amounts({"청년": "지출액의 30%", "다자녀": "지출액의 90%", "지급 방식": "월 10만원"})
    by = {t["label"]: t for t in out["tiers"]}
    assert by["청년"]["amount"] == "지출액의 30%" and by["청년"]["amount_basis"] == bt.SOURCE
    assert by["다자녀"]["amount"] == "" and by["지급 방식"]["amount"] == ""        # 원문에 없는 숫자·근거 없는 항목은 버린다


def test_web_amounts_fill_only_verified_items(monkeypatch):
    from src.generate import web_evidence as we
    seen = {}

    def fake_search(name, labels):
        seen["labels"] = labels
        return ({"items": [
            {"index": 1, "amount": "월 최대 5만원", "quote": "월 최대 5만원을 환급한다", "url": "https://www.molit.go.kr/a"},
            {"index": 2, "amount": "월 9만원", "quote": "월 5만원", "url": "https://www.molit.go.kr/a"},            # 숫자가 근거에 없다
            {"index": 3, "amount": "월 7만원", "quote": "월 7만원", "url": "https://blog.example.com/x"},            # 공식 도메인 아님
        ]}, {"https://www.molit.go.kr/a", "https://blog.example.com/x"})

    monkeypatch.setenv("WEB_LOOKUP", "1")
    monkeypatch.setattr(we.config, "OPENAI_API_KEY", "k")
    monkeypatch.setattr(we, "_search", fake_search)
    result = _amounts({"청년": "지출액의 30%"})
    bt.add_web_amounts(result, "K-패스")
    by = {t["label"]: t for t in result["tiers"]}
    assert by["청년"]["amount_basis"] == bt.SOURCE and len(seen["labels"]) == 4        # 금액이 이미 있는 항목은 조회하지 않는다
    web = [t for t in result["tiers"] if t["amount_basis"] == "web"]
    assert [t["label"] for t in web] == ["일반"] and web[0]["evidence"][0]["url"] == "https://www.molit.go.kr/a"
    assert [t["label"] for t in result["tiers"] if not t["amount"]] == ["저소득", "다자녀", "지급 방식"]


def test_web_lookup_is_off_without_key_or_when_disabled_and_failure_is_empty(monkeypatch):
    from src.generate import web_evidence as we
    monkeypatch.setattr(we.config, "OPENAI_API_KEY", "k")
    assert we.lookup("x", ["a"]) == {}                                             # WEB_LOOKUP=0
    monkeypatch.setenv("WEB_LOOKUP", "1")
    monkeypatch.setattr(we, "_search", lambda *a: (_ for _ in ()).throw(RuntimeError("no tool")))
    assert we.lookup("x", ["a"]) == {}
    assert we.allowed("https://www.gov.kr/x") and we.allowed("https://korea.kr") and not we.allowed("https://evilgo.kr/x") \
        and not we.allowed("javascript:alert(1)")


def _click(at, label):
    next(b for b in at.button if b.label == label).click().run()


def test_card_flow_requires_check_done_before_showing_support_and_amounts(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    result = _amounts({"일반": "지출액의 20%", "청년": "지출액의 30%"})
    monkeypatch.setattr(bt, "compute", lambda *a, **k: result)
    at = AppTest.from_function(_card_script, default_timeout=30).run()
    assert not at.exception
    keys = [c.key for c in at.checkbox if c.key.startswith("chk_t1_")]
    assert len(keys) == 4 and any(b.label == "체크 완료" for b in at.button)
    assert not any("내 조건으로" in b.label for b in at.button)                   # 체크 완료 전에는 버튼이 없다
    for c in at.checkbox:
        if c.key in (keys[0], keys[1]):
            c.check()
    _click(at, "체크 완료")
    assert not at.exception and any("내 조건으로 받을 수 있는 지원 보기" in b.label for b in at.button)
    _click(at, "🎁 내 조건으로 받을 수 있는 지원 보기")
    text = " ".join(m.value for m in at.markdown)
    assert "**일반** — 대중교통비의 20% 환급" in text and "**청년** — 대중교통비의 30% 환급" in text
    assert "💰 **받을 수 있는 금액: 지출액의 30%**" in text
    assert any("금액을 확인하지 못했어요" in c.value for c in at.caption)                 # 지급 방식은 금액 없음
    assert any("자세히 보기 (금액 근거)" in e.label for e in at.expander)
    assert any("조건을 더 충족하면 받을 수 있는 지원 2가지" in e.label for e in at.expander)
    assert any("자료 밖 정보" in m.value for m in at.markdown)
    next(c for c in at.checkbox if c.key == keys[2]).check().run()                   # 체크를 바꾸면 결과를 닫는다
    assert not any("받을 수 있는 금액" in m.value for m in at.markdown) and any("다시 '체크 완료'" in c.value for c in at.caption)
    _click(at, "체크 완료")
    assert any("내 조건으로 받을 수 있는 지원 보기" in b.label for b in at.button)


def test_card_panel_failure_keeps_the_normal_card(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(bt, "compute", lambda *a, **k: None)
    at = AppTest.from_function(_card_script, default_timeout=30).run()
    _click(at, "체크 완료")
    _click(at, "🎁 내 조건으로 받을 수 있는 지원 보기")
    assert not at.exception and any("정리하지 못했어요" in w.value for w in at.warning)
    assert len([c for c in at.checkbox if c.key.startswith("chk_t1_")]) == 4     # 체크리스트는 그대로 있다

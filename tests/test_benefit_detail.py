"""W2 only: SYNTHETIC evidence, pure extraction and isolated Streamlit cards."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
import requests
from streamlit.testing.v1 import AppTest

from app import card_benefit
from src.common.schema import Chunk, Citation, Recommendation
from src.generate import benefit_detail


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """An accidental lookup/model/API call fails, never becomes a skeleton skip."""
    def forbidden(*args, **kwargs):
        pytest.fail("W2 must use supplied evidence without external work")

    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("GOV24_API_KEY", "")
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    from src.index import embedder, vector_store
    from src.ingest import store
    for name in ("get_service_chunks", "build_index", "_collection"):
        monkeypatch.setattr(vector_store, name, forbidden)
    for name in ("embed_texts", "embed_query"):
        monkeypatch.setattr(embedder, name, forbidden)
    monkeypatch.setattr(store, "get_record", forbidden)


def chunk(body, suffix="", *, sid="S1", name="합성 지원", section="지원내용", prefixed=True):
    return Chunk(f"{sid}#{section}{suffix}", sid, name, section,
                 name + "\n" + body if prefixed else body,
                 {"agency": "합성 기관"}, "https://example.invalid/benefit")


def extract(chunks):
    try:
        return benefit_detail.extract(chunks)
    except NotImplementedError:
        pytest.fail("Completed W2 extraction cannot be a NotImplementedError skip")


def render(rec, key):
    try:
        return card_benefit.render(rec, key)
    except NotImplementedError:
        pytest.fail("Completed W2 rendering cannot be a NotImplementedError skip")


def rec(**kw):
    return Recommendation("합성 지원", "합성 기관", "조건 확인", "기존 요약", "기존 신청방법", **kw)


def test_preserves_alternative_types_parent_headings_and_all_shared_conditions():
    body = (
        "지원내용\nⅠ유형 — 구직활동 이행 시\n- 월 60~100만원, 6개월 지급\n"
        "- 가족수당: 1인당 10만원, 월 최대 40만원\n\n"
        "Ⅱ유형 — 훈련 참여 시\n- 일 5천 원, 최대 10회 지원\n"
        "※ 미참여일은 미지급. 다른 수당 수급 시 감액될 수 있음.\n"
        "※ 두 유형 동시 수급 불가. 지급 전 해당 조건 확인.\n"
    )
    assert extract([chunk(body)]) == [body]
    assert "최대 100만원" not in extract([chunk(body)])[0]


def test_no_line_limit_or_condition_dropping():
    body = "공통 지급 조건\n" + "수당 지급 전 출석 확인\n" * 120 + "회당 20만원 지급\n※ 중도 포기 시 미지급"
    assert extract([chunk(body)]) == [body]


def test_only_support_content_supplies_amounts():
    evidence = [chunk("만 30세, 소득 500만원 이하", section="지원대상"),
                chunk("재산 4억원 이하", section="선정기준"),
                chunk("2026년 10월 3회 방문, 수수료 1만원", section="신청방법"),
                chunk("직업 상담과 훈련 연계 지원")]
    assert extract(evidence) == []
    evidence[-1] = chunk("구직활동 이행 시 월 60만원 지급")
    assert extract(evidence) == ["구직활동 이행 시 월 60만원 지급"]


@pytest.mark.parametrize("body", ["", "  \n", "자료에 없음", "참여수당 및 인센티브 제공",
    "2026년 10월부터 6개월간 10회 지원", "참여율 80% 이상이면 수당 지급",
    "연소득 3000만원 이하인 경우 상담 지원", "사업 예산 1억원으로 상담 지원",
    "한글 수사만 있는 수당 오십만원 지급", "연도 2026, 일수 30, 비율 50%"])
def test_no_supported_monetary_detail(body):
    assert extract([chunk(body)]) == []


@pytest.mark.parametrize("body", ["월 50만 원 지급", "1회 500,000원 지원", "일 2.5만원 수당",
    "수당\n월 60만원\n출석 시 지급, 결석일 미지급", "보증금 대출 1억원 지원",
    "소득 100만원 이하인 경우 수당 20만원 지급", "이용료 30,000원 감면"])
def test_explicit_currency_units_are_preserved_without_conversion(body):
    assert extract([chunk(body)]) == [body]


@pytest.mark.parametrize("body", [
    "소득 100만원 이하인 가구에 월 20만원 지급",
    "참여 시 월 50만원 지급",
    "참여수당 월 20만원",
    "참여 시 지급액: 월 20만원",
    "소득\n100만원 이하인 가구에\n월 20만원 지급",
    "재산 2억원 미만인 가구에 회당 10만원 지원. 미참여 시 미지급.",
    "Ⅰ유형\n소득 100만원 이하: 월 20만원 지급\n※ 출석 충족 시에만 지급",
    "월 20만원 이하 지급",
    "최대 20만원 이하로 지원. 출석일에 따라 감액.",
])
def test_payment_context_is_local_to_each_amount_and_source_is_unchanged(body):
    evidence = [chunk(body)]
    before = deepcopy(evidence)
    for _ in range(2):
        assert extract(evidence) == [body]
    assert evidence == before


@pytest.mark.parametrize("body", [
    "지원 대상: 소득\n100만원 이하",
    "지원내용\n가구 소득\n100만원 이하인 경우 상담 지원",
    "지원 안내\n자산\n2억원 미만인 가구에 상담 제공",
    "지원 안내\n100만원 이하 소득인 가구에 상담 제공",
    "소득 100만원 이하인 가구는 수당 지급 대상",
    "소득 100만원 이하. 참여 시 수당 지급",
    "지원내용\n소득 100만원 이하, 재산 2억원 미만인 경우 수당 지급",
    "소득 100만원 이하\n※ 조건 충족 시 지급",
    "지원 안내\n100만원 이하인 가구에 상담 지원",
])
def test_thresholds_do_not_borrow_generic_support_or_later_payment_words(body):
    assert extract([chunk(body)]) == []


def test_threshold_support_content_cannot_borrow_payment_from_other_sections():
    assert extract([
        chunk("지원 대상: 소득\n100만원 이하"),
        chunk("참여 시 월 50만원 지급", section="지원대상"),
        chunk("수당 20만원 지급", section="신청방법"),
    ]) == []


def test_numeric_order_1_2_10_and_exact_duplicate_ids():
    parts = ["유형별 지원\n월 ", "12", "3", ",", "4", "5", "6", "원", " 지급\n", "※ 출석 시에만 지급"]
    evidence = [chunk(body, f"-{i}") for i, body in enumerate(parts, 1)]
    lexical = sorted(evidence, key=lambda c: c.chunk_id)
    lexical.append(deepcopy(evidence[0]))
    assert extract(lexical) == ["".join(parts)]
    assert extract(list(reversed(lexical))) == extract(lexical)


@pytest.mark.parametrize("budget", [1, 2, 3, 5, 7, 11, 17])
def test_real_lossless_chunker_round_trip_even_inside_amount_unit_or_condition(budget):
    from src.ingest.chunker import chunk_service
    name = "합성 지원"
    body = "유형 A\n월 123,456원, 6개월 지급\n\n※ 출석 충족 시만 지급. 미이행자는 미지급."
    service = {"service_id": "S1", "service_name": name, "support_content": body,
               "agency": "합성 기관", "detail_url": "https://example.invalid/benefit"}
    evidence = chunk_service(service, max_chars=len(name) + 1 + budget)
    assert extract(sorted(evidence, key=lambda c: c.chunk_id)) == [body]


def test_known_prefix_is_removed_once_not_globally():
    body = "합성 지원\n월 10만원 지급\n합성 지원 참여자는 출석 필요"
    assert extract([chunk(body)]) == [body]
    assert extract([chunk(body, prefixed=False)]) == [body.removeprefix("합성 지원\n")]
    literal = "다른 접두\n합성 지원 참여 시 월 10만원 지급"
    assert extract([chunk(literal, prefixed=False)]) == [literal]
    assert extract([replace(chunk(body), text="합성 지원\r\n" + body)]) == [body]


def test_input_is_unchanged_and_output_is_exact_reconstructed_source():
    evidence = [chunk("수당 월 5", "-1"), chunk("0만", "-2"), chunk(" 원\n출석 조건 충족 시만 지급", "-3")]
    before = deepcopy(evidence)
    for _ in range(3):
        assert extract(evidence) == ["수당 월 50만 원\n출석 조건 충족 시만 지급"]
    assert evidence == before


@pytest.mark.parametrize("evidence", [
    [], [chunk("나이 30세", section="지원대상")],
    [chunk("수당 월 5", "-1"), chunk("만원 지급", "-3")],
    [chunk("월 50만원 지급", "-2")],
    [chunk("월 50만원 지급"), chunk("월 60만원 지급")],
    [chunk("월 50만원 지급"), chunk("조건", "-1")],
    [chunk("월 50만원 지급", "-0")], [chunk("월 50만원 지급", "-01")],
    [replace(chunk("월 50만원 지급"), chunk_id="OTHER#지원내용")],
    [chunk("월 50만원 지급", "-1"), chunk("", "-2")],
    [chunk("월 50만원 지급"), replace(chunk("월 50만원 지급"), detail_url="https://example.invalid/conflict")],
    [chunk("월 50만원 지급"), chunk("월 900만원 지급", sid="OTHER")],
    [chunk("월 50만원 지급"), chunk("상담", sid="OTHER", section="신청방법")],
    [chunk("월 50만원 지급"), chunk("동일 ID의 다른 명칭", name="다른 지원", section="지원대상")],
    [chunk("월 50만원 지급", sid="")], [chunk("월 50만원 지급", name="")],
])
def test_incomplete_conflicting_or_mixed_evidence_cannot_manufacture_detail(evidence):
    assert extract(evidence) == []


class DisplayDouble:
    def __init__(self):
        self.calls = []

    def container(self, **kw):
        self.calls.append(("container", kw))
        return nullcontext()

    def expander(self, label, **kw):
        self.calls.append(("expander", label, kw))
        return nullcontext()

    def caption(self, text):
        self.calls.append(("caption", text))

    def text(self, text):
        self.calls.append(("text", text))


@pytest.mark.parametrize("details", [[], ["", " \n"]])
def test_empty_details_draw_nothing_and_preserve_summary(monkeypatch, details):
    ui = DisplayDouble()
    monkeypatch.setattr(card_benefit, "st", ui)
    r = rec(benefit_detail=details)
    before = deepcopy(r)
    assert render(r, "one") is None
    assert ui.calls == [] and r == before


def test_render_keeps_source_literal_collapsed_and_card_scoped(monkeypatch):
    ui = DisplayDouble()
    monkeypatch.setattr(card_benefit, "st", ui)
    body = "유형 A\n<script>alert('금액')</script> [링크](javascript:alert(1))\n월 50만원 지급"
    r = rec(benefit_detail=[body, "※ 결석 시 미지급"], citations=[Citation("s", "a", "원문", "https://example.invalid")])
    before = deepcopy(r)
    assert render(r, "first") is None
    assert render(r, "second") is None
    assert [c[1]["key"] for c in ui.calls if c[0] == "container"] == ["card_benefit_first", "card_benefit_second"]
    assert all(c[2] == {"expanded": False} for c in ui.calls if c[0] == "expander")
    assert [c[1] for c in ui.calls if c[0] == "text"] == [body, "※ 결석 시 미지급"] * 2
    assert all("기존 요약" not in str(c) for c in ui.calls)
    assert r == before


def _cards_script():
    import streamlit as st
    from app import main as app
    from src.common.schema import Citation, Recommendation

    st.session_state.setdefault("checks", {})
    st.session_state.setdefault("guides", {})
    for i, amount in enumerate(["월 20만원 지급\n※ 출석 시만", "회당 10만원 지원\n※ 중복 수급 불가"], 1):
        r = Recommendation(f"합성 카드 {i}", "합성 기관", "조건 확인", f"기존 요약 {i}", "신청방법",
                           benefit_detail=[amount + "\n<b>원문 표기</b>"],
                           citations=[Citation(f"합성 카드 {i}", "합성 기관", amount, "https://example.invalid/source")])
        app._card(i, r, f"w2_{i}")


def test_actual_streamlit_card_preserves_summary_sources_and_separate_detail_areas():
    at = AppTest.from_function(_cards_script).run()
    assert not at.exception
    assert len(at.expander) == 2
    assert all(e.label == "무엇을 받나요 — 자세히 보기" and not e.proto.expanded for e in at.expander)
    assert "월 20만원" in at.expander[0].text[0].value
    assert "회당 10만원" not in at.expander[0].text[0].value
    assert "회당 10만원" in at.expander[1].text[0].value
    assert "<b>원문 표기</b>" in at.expander[1].text[0].value
    markup = "\n".join(m.value for m in at.markdown)
    assert "기존 요약 1" in markup and "기존 요약 2" in markup
    assert markup.count("근거 원문 1건 보기") == 2
    assert "https://example.invalid/source" in markup
    at.run()
    assert not at.exception and len(at.expander) == 2
    assert "월 20만원" in at.expander[0].text[0].value


def test_existing_generator_contract_uses_input_evidence_without_extra_calls(monkeypatch):
    from src.generate import generator
    monkeypatch.setattr(generator, "_application_period", lambda sid: "")
    evidence = [chunk("I유형\n구직활동 이행 시 월 60만원 지급\n※ 미이행 시 미지급")]
    r = generator._to_recommendation("지원 안내", evidence, {"benefit": "원래 한 줄 요약"})
    assert r.benefit == "원래 한 줄 요약"
    assert r.benefit_detail == [evidence[0].text.removeprefix("합성 지원\n")]
    assert r.citations and r.citations[0].detail_url == evidence[0].detail_url
    assert r.service_id == "S1"


def test_recommendation_defaults_remain_independent():
    a, b = rec(), rec()
    a.benefit_detail.append("수당 월 1만원 지급")
    assert b.benefit_detail == [] and a.benefit == b.benefit == "기존 요약"
    assert a.apply_url == b.apply_url == "" and a.period_detail == b.period_detail == ""

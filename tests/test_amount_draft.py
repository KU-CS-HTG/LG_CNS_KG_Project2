"""범용 계산 엔진(amount_rules.generic_calculate)과 규칙 초안 만들기(amount_draft). LLM은 가짜로 바꿔 끼운다."""
import json
from datetime import date

import pytest

from src.generate import amount_draft as ad
from src.generate import amount_rules as ar

SERVICE = {"service_id": "S900", "service_name": "청년 월세 지원", "agency": "국토교통부", "detail_url": "https://www.gov.kr/portal/x",
           "support_target": "만 19~34세 무주택 청년으로 기준 중위소득 60% 이하",
           "selection_criteria": "부모와 따로 거주하는 청년",
           "support_content": "월 최대 20만원을 최대 24개월간 지원합니다.\n월세의 80%를 지원하되 월 10만원 한도입니다."}
GOOD = {
    "inputs": [{"id": "age", "label": "만 나이", "type": "int", "min": 0, "max": 120},
               {"id": "no_house", "label": "무주택자인가요?", "type": "bool"},
               {"id": "size", "label": "가구원 수", "type": "int", "min": 1, "max": 10},
               {"id": "income", "label": "가구 월소득 (만원)", "type": "int", "unit_won": 10000, "min": 0},
               {"id": "rent", "label": "월세 (만원)", "type": "int", "unit_won": 10000, "min": 0}],
    "variants": [
        {"name": "청년 월세 지원", "quote": "월 최대 20만원을 최대 24개월간 지원합니다",
         "eligibility": [{"type": "range", "input": "age", "min": 19, "max": 34, "label": "만 19~34세"},
                         {"type": "is_true", "input": "no_house", "label": "무주택"},
                         {"type": "median_income", "pct": 60, "income_input": "income", "size_input": "size", "label": "중위소득 60% 이하"}],
         "payout": {"type": "fixed", "amount_won": 200000, "months": 24}},
        {"name": "월세 비율 지원", "quote": "월세의 80%를 지원하되 월 10만원 한도입니다",
         "eligibility": [{"type": "is_true", "input": "no_house", "label": "무주택"}],
         "payout": {"type": "percent", "rate": 0.8, "base_input": "rent", "cap_won": 100000, "months": 24}}]}


def rule():
    r, issues = ad.draft(SERVICE, lambda s, u: GOOD, today=date(2026, 10, 28))
    assert r and issues == []
    return r


def test_draft_keeps_verified_rule_and_fills_identity_by_code():
    r = rule()
    assert r["service_ids"] == ["S900"] and r["calculator"] == "generic" and r["status"] == ad.STATUS and r["as_of"] == "2026-10-28"
    assert r["sources"][0]["url"] == "https://www.gov.kr/portal/x" and r["facts"]["median_income_monthly_won"]["1"] == 2564238
    assert [v["name"] for v in r["variants"]] == ["청년 월세 지원", "월세 비율 지원"]


def test_draft_drops_variants_with_numbers_or_quotes_missing_from_the_source():
    import copy
    bad = copy.deepcopy(GOOD)
    bad["variants"][0]["payout"]["amount_won"] = 300000                       # 원문에 30만원 없음
    bad["variants"][1]["eligibility"].append({"type": "range", "input": "age", "min": 19, "max": 39, "label": "만 19~39세"})   # 39 없음
    bad["variants"].append({"name": "지어낸 유형", "quote": "원문에 없는 문장입니다", "eligibility": [], "payout": {"type": "fixed", "amount_won": 200000}})
    r, issues = ad.draft(SERVICE, lambda s, u: bad)
    assert r is None and any("30" in i or "300000" in i for i in issues) and any("39" in i for i in issues) and any("글자 그대로" in i for i in issues)


def test_draft_keeps_the_good_variant_when_another_is_dropped():
    import copy
    mixed = copy.deepcopy(GOOD)
    mixed["variants"][0]["payout"]["months"] = 36                              # 36개월은 원문에 없다
    r, issues = ad.draft(SERVICE, lambda s, u: mixed)
    assert [v["name"] for v in r["variants"]] == ["월세 비율 지원"] and any("36" in i for i in issues)
    assert {s["id"] for s in r["inputs"]} == {"no_house", "rent"}               # 쓰이지 않는 입력은 뺀다
    assert any("월세 비율 지원" not in o and "36" in o for o in r["open_issues"])


def test_draft_returns_none_when_nothing_is_calculable():
    assert ad.draft(SERVICE, lambda s, u: {"inputs": [], "variants": []})[0] is None


def test_source_numbers_parses_korean_amounts():
    nums = ad.source_numbers("월 20만원, 보증금 1억 2천만원, 연 3.5%, 2년")
    assert {200000, 120000000, 3.5, 24}.issubset(nums)


def test_generic_calculate_fixed_percent_and_missing_values():
    r = rule()
    full = ar.generic_calculate(r, {"age": 26, "no_house": True, "size": 1, "income": 100, "rent": 40})
    by = {s["title"]: s for s in full["scenarios"]}
    assert by["청년 월세 지원"]["amount_won"] == 200000 * 24 and by["월세 비율 지원"]["amount_won"] == 100000 * 24     # 40만×80%=32만 -> 한도 10만
    assert full["scenarios"][0]["title"] == "청년 월세 지원" and any("가장 커요" in x for x in full["scenarios"][0]["lines"])
    part = ar.generic_calculate(r, {"age": 26, "no_house": True, "size": 1, "income": None, "rent": None})
    assert {s["title"]: s["verdict"] for s in part["scenarios"]} == {"청년 월세 지원": "확인 필요", "월세 비율 지원": "확인 필요"}
    no = ar.generic_calculate(r, {"age": 40, "no_house": True, "size": 1, "income": 100, "rent": 40})
    young_fail = {s["title"]: s for s in no["scenarios"]}["청년 월세 지원"]
    assert young_fail["verdict"] == "해당 어려움" and "만 19~34세" in young_fail["reasons"][0]
    rich = ar.generic_calculate(r, {"age": 26, "no_house": True, "size": 1, "income": 400, "rent": 40})
    assert {s["title"]: s["verdict"] for s in rich["scenarios"]}["청년 월세 지원"] == "해당 어려움"
    big = ar.generic_calculate(r, {"age": 26, "no_house": True, "size": 6, "income": 100, "rent": 40})
    assert {s["title"]: s["verdict"] for s in big["scenarios"]}["청년 월세 지원"] == "확인 필요"        # 6인 기준중위소득은 규칙에 없다


def test_generic_table_adders_and_auto_flags():
    r = {"inputs": [{"id": "size", "label": "가구원 수", "type": "int"}, {"id": "kids", "label": "자녀 수", "type": "int"},
                    {"id": "low", "label": "저소득", "type": "bool", "from_check": "저소득|차상위"}],
         "variants": [{"name": "생계비", "quote": "x", "eligibility": [{"type": "is_true", "input": "low", "label": "저소득"}],
                       "payout": {"type": "table", "by": "size", "rows": [{"min": 1, "max": 1, "amount_won": 380000, "label": "1인"}, {"min": 2, "max": 2, "amount_won": 620000, "label": "2인"}], "months": 12},
                       "adders": [{"input": "kids", "per_won": 50000, "max_won": 100000, "label": "자녀 가산"}]}],
         "sources": [], "as_of": "d", "status": "s"}
    flags = ar.auto_flags(r, ["소득 — 차상위계층 이하", "재산 — 1억"], {1})
    assert flags == {"low": True} and ar.auto_flags(r, ["재산 — 1억"], {1}) == {}
    out = ar.generic_calculate(r, {**flags, "size": 2, "kids": 3})["scenarios"][0]
    assert out["amount_won"] == (620000 + 100000) * 12 and any("자녀 가산" in x for x in out["lines"])    # 가산은 상한 10만 원
    assert ar.generic_calculate(r, {"low": True, "size": 5, "kids": 0})["scenarios"][0]["verdict"] == "확인 필요"       # 구간표에 없는 값


def test_promote_writes_validated_draft_and_rejects_duplicates(tmp_path):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": [{"service_ids": ["OLD"]}]}), encoding="utf-8")
    d = tmp_path / "S900.json"
    d.write_text(json.dumps(rule(), ensure_ascii=False), encoding="utf-8")
    assert ad.promote(d, rules) == "청년 월세 지원"
    assert [r["service_ids"] for r in json.loads(rules.read_text(encoding="utf-8"))["rules"]] == [["OLD"], ["S900"]]
    with pytest.raises(ValueError, match="이미 규칙"):
        ad.promote(d, rules)
    d.write_text(json.dumps({"service_ids": ["Z"], "calculator": "kupj"}), encoding="utf-8")
    with pytest.raises(ValueError):
        ad.promote(d, rules)


def test_cli_draft_from_raw_file(tmp_path, monkeypatch, capsys):
    raw = tmp_path / "serviceList.json"
    raw.write_text(json.dumps([{"서비스ID": "S900", "서비스명": "청년 월세 지원", "소관기관명": "국토교통부", "상세조회URL": "https://www.gov.kr/portal/x",
                                "지원대상": SERVICE["support_target"], "선정기준": SERVICE["selection_criteria"], "지원내용": SERVICE["support_content"]}], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", lambda s, u: GOOD)
    out = tmp_path / "drafts"
    assert ad.main(["draft", "--service-id", "S900", "--raw", str(raw), "--out", str(out)]) == 0
    saved = json.loads((out / "S900.json").read_text(encoding="utf-8"))
    assert saved["service_ids"] == ["S900"] and "promote" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        ad.main(["draft", "--service-id", "없는ID", "--raw", str(raw), "--out", str(out)])


def _card_script():
    import streamlit as st
    from app import main as app
    from src.common.schema import Recommendation

    st.session_state.setdefault("checks", {})
    st.session_state.setdefault("guides", {})
    r = Recommendation("청년 월세 지원", "국토교통부", "조건 확인", "월세 지원", "복지로", service_id="S900", needs_check=["주택 — 무주택 청년"])
    app._card(1, r, "g1")


def test_card_renders_generic_rule_and_calculates_live(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    from src.generate import benefit_tiers

    r = rule()
    r["inputs"][1]["from_check"] = "무주택"
    monkeypatch.setattr(ar, "_load", lambda: [r])
    monkeypatch.setattr(benefit_tiers, "compute", lambda *a, **k: {"tiers": [{"label": "월세", "requires": [], "benefit": "월세 지원", "quote": "", "basis": "outside",
                                                                            "amount": "", "amount_basis": "", "evidence": []}], "notes": []})
    at = AppTest.from_function(_card_script, default_timeout=30).run()
    next(c for c in at.checkbox if c.key == "chk_g1_0").check()
    next(b for b in at.button if b.label == "체크 완료").click().run()
    next(b for b in at.button if "내 조건으로" in b.label).click().run()
    assert not at.exception
    assert any("체크리스트 답으로 채운 항목" in c.value and "무주택자인가요? → 예" in c.value for c in at.caption)
    assert not any("무주택자인가요?" in x.label for x in at.radio)                    # 체크로 알 수 있는 입력은 다시 묻지 않는다
    at.number_input(key="amt_g1_age").set_value(26).run()
    at.number_input(key="amt_g1_size").set_value(1).run()
    at.number_input(key="amt_g1_income").set_value(100).run()
    at.number_input(key="amt_g1_rent").set_value(40).run()
    text = " ".join(m.value for m in at.markdown)
    assert not at.exception and "예상 수령액: 480만 원" in text and "청년 월세 지원" in text

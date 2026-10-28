"""사람이 공식 자료로 예상 수령액 규칙을 넣는 도구(amount_manual)와 화면(app/amount_mode.py)."""
import json
import os
from datetime import date

import pytest

from src.generate import amount_manual as am
from src.generate import amount_rules as ar

NOMONEY = {"service_id": "S50", "service_name": "가상 생활안정 지원", "agency": "고용노동부", "field": "고용", "support_target": "청년",
           "selection_criteria": "", "support_content": "취업을 위한 생활안정 지원금을 지급합니다.", "how_to_apply": "고용센터 방문",
           "application_period": "상시", "detail_url": "https://www.gov.kr/x"}
MONEY = {**NOMONEY, "service_id": "S51", "service_name": "정액 수당", "support_content": "매월 30만원을 지급합니다."}
LOCAL = {**NOMONEY, "service_id": "S52", "service_name": "구청 지원", "agency": "서울특별시 종로구"}
FORM = {"variants": [{"name": "생활안정 지원", "pay_type": "fixed", "amount_won": 500000, "months": 6, "age_min": 19, "age_max": 34, "median_pct": 0, "no_house": False}],
        "source": {"title": "2026년 사업 안내", "url": "https://www.moel.go.kr/x", "level": "공식", "quote": "월 50만원씩 6개월간 지급한다"}, "note": "선발형"}


def test_needs_official_lists_services_without_amounts_first_by_demand():
    rows = am.needs_official([NOMONEY, MONEY, LOCAL, {**NOMONEY, "service_id": "S53", "service_name": "다른 지원"}], {"S53": 3})
    assert [r["service_id"] for r in rows] == ["S53", "S50"]               # 금액 있는 제도·지자체 제외, 수요 많은 순
    assert [r["service_id"] for r in am.needs_official([NOMONEY, MONEY, LOCAL], include_local=True)] == ["S50", "S52"]
    assert {r["service_id"] for r in am.needs_official([NOMONEY, MONEY], scope="all")} == {"S50", "S51"}
    assert [r["service_id"] for r in am.needs_official([NOMONEY, MONEY], scope="all", text="수당")] == ["S51"]
    kupj = am.needs_official([{**NOMONEY, "service_id": "149200005007", "service_name": "국민취업지원제도"}, NOMONEY])
    assert [r["has_rule"] for r in kupj] == [False, True]                    # 이미 규칙이 있으면 뒤로


def test_form_to_rule_builds_a_valid_generic_rule():
    rule, problems = am.form_to_rule(NOMONEY, FORM, "kim", date(2026, 10, 28))
    assert problems == [] and rule["service_ids"] == ["S50"] and rule["calculator"] == "generic"
    assert rule["variants"][0]["payout"] == {"type": "fixed", "amount_won": 500000, "months": 6, "per": "월"}
    assert rule["variants"][0]["eligibility"][0]["min"] == 19 and [i["id"] for i in rule["inputs"]] == ["age"]
    assert rule["sources"][0]["level"] == "공식(담당자 입력)" and "kim" in rule["status"] and rule["open_issues"][0] == "선발형"
    out = ar.generic_calculate(rule, {"age": 26})["scenarios"][0]
    assert out["amount_won"] == 3_000_000 and ar.generic_calculate(rule, {"age": 40})["scenarios"][0]["verdict"] == "해당 어려움"


def test_form_to_rule_percent_median_and_second_level_status():
    form = {"variants": [{"name": "월세 지원", "pay_type": "percent", "rate_pct": 80, "cap_won": 100000, "months": 12, "age_min": 0, "age_max": 0, "median_pct": 60, "no_house": True}],
            "source": {"title": "복지로 안내", "url": "https://www.bokjiro.go.kr/x", "level": "2차", "quote": "월세의 80%를 월 10만원 한도로 지원"}}
    rule, problems = am.form_to_rule(NOMONEY, form)
    assert problems == [] and "공식 원문 대조 필요" in rule["status"] and rule["facts"]["median_income_monthly_won"]["1"] == 2564238
    assert {i["id"] for i in rule["inputs"]} == {"cost", "income", "size", "no_house"}
    out = ar.generic_calculate(rule, {"cost": 40, "income": 100, "size": 1, "no_house": True})["scenarios"][0]
    assert out["amount_won"] == 100000 * 12


def test_form_to_rule_reports_every_missing_piece():
    _, problems = am.form_to_rule(NOMONEY, {"variants": [{"name": "", "pay_type": "fixed"}], "source": {}})
    text = " ".join(problems)
    assert "제목" in text and "주소" in text and "구절" in text and "등급" in text and "이름" in text
    bad = {**FORM, "variants": [{**FORM["variants"][0], "amount_won": 0}], "source": {**FORM["source"], "url": "javascript:alert(1)"}}
    assert any("금액" in p for p in am.form_to_rule(NOMONEY, bad)[1]) and any("주소" in p for p in am.form_to_rule(NOMONEY, bad)[1])
    assert any("나이" in p for p in am.form_to_rule(NOMONEY, {**FORM, "variants": [{**FORM["variants"][0], "age_min": 40, "age_max": 30}]})[1])


def test_check_rule_rejects_broken_handwritten_json():
    rule, _ = am.form_to_rule(NOMONEY, FORM)
    assert am.check_rule(rule) == []
    broken = json.loads(json.dumps(rule))
    broken["variants"][0]["eligibility"][0]["input"] = "없는입력"
    broken["variants"][0]["payout"]["type"] = "magic"
    broken["sources"][0]["url"] = ""
    problems = " ".join(am.check_rule(broken))
    assert "조건" in problems and "지급 방식" in problems and "출처" in problems
    assert am.check_rule({"calculator": "kupj"}) == ["calculator는 generic이어야 해요."]


def test_save_rule_adds_replaces_and_protects_code_rules(tmp_path, monkeypatch):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"rules": [{"service_ids": ["K"], "name": "코드 규칙", "calculator": "kupj"}]}), encoding="utf-8")
    monkeypatch.setattr(ar, "RULES_PATH", path)
    rule, _ = am.form_to_rule(NOMONEY, FORM)
    assert am.save_rule(rule) == "added" and ar.find("S50")["name"] == "가상 생활안정 지원"
    changed = json.loads(json.dumps(rule))
    changed["variants"][0]["payout"]["amount_won"] = 600000
    os.utime(path, ns=(1, 1))                                              # 같은 시각이어도 아래 저장이 새로 읽히는지는 cache_clear가 보장
    assert am.save_rule(changed) == "replaced" and ar.find("S50")["variants"][0]["payout"]["amount_won"] == 600000
    assert len(json.loads(path.read_text(encoding="utf-8"))["rules"]) == 2
    clash, _ = am.form_to_rule({**NOMONEY, "service_id": "K"}, FORM)
    with pytest.raises(ValueError, match="코드로 계산"):
        am.save_rule(clash)
    with pytest.raises(ValueError):
        am.save_rule({"calculator": "generic"})


def test_rules_file_changes_are_picked_up_without_restart(tmp_path, monkeypatch):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"rules": []}), encoding="utf-8")
    monkeypatch.setattr(ar, "RULES_PATH", path)
    assert ar.find("S50") is None
    rule, _ = am.form_to_rule(NOMONEY, FORM)
    path.write_text(json.dumps({"rules": [rule]}), encoding="utf-8")        # 다른 프로세스가 파일을 바꾼 것과 같다
    os.utime(path, ns=(2_000_000_000, 2_000_000_000))
    assert ar.find("S50")["name"] == "가상 생활안정 지원"


def test_preview_rule_is_scoped_to_the_block():
    rule, _ = am.form_to_rule(NOMONEY, FORM)
    assert ar.find("S50") is None
    with ar.preview_rule(rule):
        assert ar.find("S50") is rule and ar.find("다른") is None
    assert ar.find("S50") is None


def test_preview_helpers_are_deterministic_and_use_source_text():
    r = am.preview_recommendation(NOMONEY, ["주택 — 무주택"])
    assert r.service_id == "S50" and r.benefit_detail == [NOMONEY["support_content"]] and r.needs_check == ["주택 — 무주택"]
    assert r.citations[0].detail_url == "https://www.gov.kr/x" and "상시" in r.how_to_apply
    t = am.preview_tiers(NOMONEY)["tiers"][0]
    assert t["quote"] in NOMONEY["support_content"] and t["amount"] == "" and t["basis"] == "source"


def _page(tmp_path):
    script = tmp_path / "wrap.py"
    script.write_text("import app.amount_mode as r\nr.render()\n", encoding="utf-8")
    return script


def test_page_shows_before_then_after_preview_and_saves(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    import app.amount_mode as page

    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": []}), encoding="utf-8")
    monkeypatch.setattr(ar, "RULES_PATH", rules)
    monkeypatch.setattr(page, "_services", lambda: {"S50": NOMONEY, "S51": MONEY})
    monkeypatch.setattr(page, "_demand", lambda: {})
    at = AppTest.from_file(str(_page(tmp_path)), default_timeout=30).run()
    assert not at.exception and any("사람이 공식 자료를 넣어야 하는 제도 1개" in c.value for c in at.caption)    # 금액 있는 S51은 목록에 없다
    at.session_state["amount_sel_id"] = "S50"
    at.run()
    assert not at.exception
    assert any("① 지금 답변에 나오는 모습" in s.value for s in at.subheader)
    assert any("금액을 확인하지 못했어요" in c.value for c in at.caption)                  # 지금 화면: 금액 없음
    assert any("아직 미리볼 수 없어요" in i.value for i in at.info)                          # 입력 전에는 ③ 미리보기가 없다
    at.number_input(key="am_S50_1_amount").set_value(500000)
    at.number_input(key="am_S50_1_months").set_value(6)
    next(t for t in at.text_input if t.key == "am_S50_1_name").set_value("생활안정 지원")
    at.text_input(key="am_S50_stitle").set_value("2026년 사업 안내")
    at.text_input(key="am_S50_surl").set_value("https://www.moel.go.kr/x")
    at.text_area(key="am_S50_quote").set_value("월 50만원씩 6개월간 지급한다")
    at.run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "예상 수령액: 300만 원" in text                                                  # 입력 후 화면: 금액이 나온다
    assert ar.find("S50") is None                                                         # 저장 전에는 실제 규칙이 아니다
    assert next(b for b in at.button if b.label == "이 규칙으로 저장").disabled              # 이름 없이는 저장 불가
    at.sidebar.text_input[0].set_value("kim").run()
    next(b for b in at.button if b.label == "이 규칙으로 저장").click().run()
    assert any("규칙을 추가했어요" in s.value for s in at.success)
    assert ar.find("S50")["variants"][0]["payout"]["amount_won"] == 500000 and "kim" in ar.find("S50")["status"]

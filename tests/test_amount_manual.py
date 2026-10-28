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
    kupj_svc = {**NOMONEY, "service_id": "149200005007", "service_name": "국민취업지원제도"}
    assert [r["service_id"] for r in am.needs_official([kupj_svc, NOMONEY])] == ["S50"]            # 규칙을 저장한 제도는 기본 목록에서 빠진다
    shown = am.needs_official([kupj_svc, NOMONEY], show_done=True)
    assert [(r["service_id"], r["state"]) for r in shown] == [("S50", ""), ("149200005007", "규칙 저장")]    # 보기를 켜면 뒤에 상태와 함께
    assert am.needs_official([NOMONEY], done={"S50"}) == [] and am.needs_official([NOMONEY], done={"S50"}, show_done=True)[0]["state"] == "확인 완료"


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


def test_reviewed_events_use_latest_record_and_validate_names(tmp_path):
    am.mark_reviewed("S50", "kim", "현물 서비스", reviewed_dir=tmp_path)
    am.mark_reviewed("S51", "lee", reviewed_dir=tmp_path)
    assert set(am.reviewed(tmp_path)) == {"S50", "S51"} and am.reviewed(tmp_path)["S50"]["note"] == "현물 서비스"
    am.mark_reviewed("S50", "lee", undo=True, reviewed_dir=tmp_path)                    # 다른 사람이 나중에 되돌리면 빠진다
    assert set(am.reviewed(tmp_path)) == {"S51"}
    (tmp_path / "kim.jsonl").open("a", encoding="utf-8").write("깨진 줄\n")
    assert set(am.reviewed(tmp_path)) == {"S51"} and am.reviewed(tmp_path / "없음") == {}
    for bad in ("", "../x", "김"):
        with pytest.raises(ValueError):
            am.mark_reviewed("S50", bad, reviewed_dir=tmp_path)


def test_page_hides_reviewed_services_and_can_restore(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    import app.amount_mode as page

    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": []}), encoding="utf-8")
    monkeypatch.setattr(ar, "RULES_PATH", rules)
    monkeypatch.setattr(am, "REVIEWED_DIR", tmp_path / "reviewed")
    monkeypatch.setattr(page, "_services", lambda: {"S50": NOMONEY, "S54": {**NOMONEY, "service_id": "S54", "service_name": "다른 지원"}})
    monkeypatch.setattr(page, "_demand", lambda: {})
    at = AppTest.from_file(str(_page(tmp_path)), default_timeout=30).run()
    assert any("제도 2개" in c.value for c in at.caption)
    at.session_state["amount_sel_id"] = "S50"
    at.run()
    assert next(b for b in at.button if "지금 답변 그대로 OK" in b.label).disabled and any("Enter" in w.value for w in at.warning)   # 이름이 없으면 이유를 알려 준다
    at.sidebar.text_input[0].set_value("김태규").run()
    assert next(b for b in at.button if "지금 답변 그대로 OK" in b.label).disabled and any("영문·숫자" in w.value for w in at.warning)   # 한글 이름도 막고 이유를 알려 준다
    at.sidebar.text_input[0].set_value("kim").run()
    next(b for b in at.button if "지금 답변 그대로 OK" in b.label).click().run()
    assert not at.exception and any("제도 1개" in c.value for c in at.caption)           # 확인 완료한 제도는 목록에서 빠진다
    assert at.session_state["amount_sel_id"] is None and set(am.reviewed()) == {"S50"}
    at.sidebar.checkbox[1].check().run()                                                  # '처리한 제도도 보기'
    assert any("제도 2개" in c.value for c in at.caption)
    at.session_state["amount_sel_id"] = "S50"
    at.run()
    assert any("확인 완료 — kim" in s.value for s in at.success)
    next(b for b in at.button if b.label == "다시 확인 필요로 되돌리기").click().run()
    assert am.reviewed() == {}


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


# --- 공식 자료 주소로 폼 채우기 (amount_from_url) ---------------------------------------------------------------------
PAGE = """<html><head><title>청년창업기업 보증 안내</title><script>var x=1;</script></head><body><nav>메뉴</nav>
<h1>청년창업기업 보증</h1><p>만 19세~34세 청년이 창업 후 7년 이내이면 보증을 받을 수 있습니다.</p>
<table><tr><th>구분</th><td>보증한도</td><td>최대 30억원</td></tr></table>
<p>보증비율은 90%이며 보증료는 연 0.5%입니다. 지원 기간은 12개월입니다.</p></body></html>"""
GOOD_FILL = {"page_matches_service": True, "match_reason": "청년창업기업 보증 설명",
             "variants": [{"name": "보증 한도(최대)", "pay_type": "fixed", "amount_won": 3_000_000_000, "months": 12, "age_min": 19, "age_max": 34,
                           "median_pct": 0, "no_house": False, "quote": "보증한도 | 최대 30억원"}],
             "note": "보증 심사를 거쳐요."}


def _fetch(url):
    from src.generate import amount_from_url as af
    return af.html_to_text(PAGE)


def test_html_to_text_drops_scripts_and_keeps_table_rows():
    from src.generate import amount_from_url as af
    title, text = af.html_to_text(PAGE)
    assert title == "청년창업기업 보증 안내" and "var x" not in text and "보증한도 | 최대 30억원" in text.replace("  ", " ")


def test_check_url_blocks_non_http_and_local_addresses():
    from src.generate import amount_from_url as af
    assert af.check_url("javascript:alert(1)") and af.check_url("ftp://x.kr/a") and af.check_url("http://127.0.0.1:8501/x") and af.check_url("http://localhost/x")
    assert af.default_level("https://www.moel.go.kr/a") == "공식" and af.default_level("https://www.kibo.or.kr/a") == "2차"


def test_fill_form_keeps_only_values_found_in_the_page():
    from src.generate import amount_from_url as af
    out = af.fill_form(NOMONEY, "https://www.kibo.or.kr/x", lambda s, u: GOOD_FILL, _fetch)
    assert out["ok"] and out["issues"] == []
    v = out["form"]["variants"][0]
    assert v["amount_won"] == 3_000_000_000 and v["age_min"] == 19 and v["age_max"] == 34 and v["months"] == 12
    assert out["form"]["source"] == {"title": "청년창업기업 보증 안내", "url": "https://www.kibo.or.kr/x", "level": "2차", "quote": "보증한도 | 최대 30억원"}
    rule, problems = am.form_to_rule(NOMONEY, out["form"], "kim")                         # 채운 폼이 그대로 규칙이 된다
    assert problems == [] and ar.generic_calculate(rule, {"age": 26})["scenarios"][0]["amount_won"] == 3_000_000_000 * 12
    assert "만 19세" in out["text"]


def test_fill_form_drops_or_blanks_unverifiable_values():
    from src.generate import amount_from_url as af
    bad = {"page_matches_service": True, "match_reason": "",
           "variants": [{"name": "지어낸 금액", "pay_type": "fixed", "amount_won": 5_000_000_000, "quote": "보증한도 | 최대 30억원"},
                        {"name": "지어낸 구절", "pay_type": "fixed", "amount_won": 3_000_000_000, "quote": "본문에 없는 문장입니다"},
                        {"name": "조건 숫자 틀림", "pay_type": "fixed", "amount_won": 3_000_000_000, "age_min": 20, "age_max": 34, "months": 99,
                         "quote": "보증한도 | 최대 30억원"},
                        {"name": "비율형", "pay_type": "percent", "rate_pct": 90, "cap_won": 7_000_000_000, "quote": "보증비율은 90%이며"}]}
    out = af.fill_form(NOMONEY, "https://www.kibo.or.kr/x", lambda s, u: bad, _fetch)
    names = [v["name"] for v in out["form"]["variants"]]
    assert names == ["조건 숫자 틀림", "비율형"]
    blank = out["form"]["variants"][0]
    assert blank["age_min"] == 0 and blank["age_max"] == 34 and blank["months"] == 0                 # 본문에 없는 20·99는 비운다
    assert out["form"]["variants"][1]["cap_won"] == 0 and out["form"]["variants"][1]["rate_pct"] == 90
    text = " ".join(out["issues"])
    assert "금액이 페이지 본문에 없어" in text and "글자 그대로 없어" in text and "나이 최소" in text and "개월 수" in text and "한도 금액" in text


def test_fill_form_refuses_other_programs_and_reports_fetch_or_ai_failures():
    from src.generate import amount_from_url as af
    other = af.fill_form(NOMONEY, "https://www.kibo.or.kr/x", lambda s, u: {"page_matches_service": False, "match_reason": "베이비유니콘 사업 설명"}, _fetch)
    assert not other["ok"] and "다른 내용" in other["message"] and "베이비유니콘" in other["message"]
    def boom(url):
        raise RuntimeError("자료를 열지 못했어요")
    assert af.fill_form(NOMONEY, "https://x.kr", lambda s, u: GOOD_FILL, boom)["message"] == "자료를 열지 못했어요"
    def ai_down(s, u):
        raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다")
    assert "AI 호출에 실패" in af.fill_form(NOMONEY, "https://x.kr", ai_down, _fetch)["message"]
    none = af.fill_form(NOMONEY, "https://x.kr", lambda s, u: {"page_matches_service": True, "variants": []}, _fetch)
    assert not none["ok"] and "직접 입력" in none["message"]


def test_page_autofill_fills_the_form_and_previews_it(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    import app.amount_mode as page
    from src.generate import amount_from_url as af

    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": []}), encoding="utf-8")
    monkeypatch.setattr(ar, "RULES_PATH", rules)
    monkeypatch.setattr(am, "REVIEWED_DIR", tmp_path / "reviewed")
    monkeypatch.setattr(page, "_services", lambda: {"S50": NOMONEY})
    monkeypatch.setattr(page, "_demand", lambda: {})
    monkeypatch.setattr(af, "fetch_page", lambda url: af.html_to_text(PAGE))
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", lambda s, u: GOOD_FILL)
    at = AppTest.from_file(str(_page(tmp_path)), default_timeout=30).run()
    at.session_state["amount_sel_id"] = "S50"
    at.run()
    assert next(b for b in at.button if b.label == "자료에서 채우기").disabled                # 주소가 없으면 꺼져 있다
    at.text_input(key="am_S50_aiurl").set_value("https://www.kibo.or.kr/x").run()
    next(b for b in at.button if b.label == "자료에서 채우기").click().run()
    assert not at.exception
    assert any("AI가 자료에서 채운 것" in i.value for i in at.info)
    assert at.text_input(key="am_S50_1_name").value == "보증 한도(최대)" and at.number_input(key="am_S50_1_amount").value == 3_000_000_000
    assert at.text_input(key="am_S50_stitle").value == "청년창업기업 보증 안내" and at.text_input(key="am_S50_surl").value == "https://www.kibo.or.kr/x"
    assert at.radio(key="am_S50_level").value.startswith("2차")                                # kibo.or.kr은 공식 도메인 목록 밖 -> 사람이 고른다
    assert at.text_area(key="am_S50_quote").value == "보증한도 | 최대 30억원"
    assert any("③ 입력 후 모습" in s.value for s in at.subheader) and not any("아직 미리볼 수 없어요" in i.value for i in at.info)    # 입력 없이 바로 미리보기
    at.number_input(key="am_S50_1_amount").set_value(2_000_000_000).run()                      # 사람은 세부 값만 고친다
    assert "예상 수령액" in " ".join(m.value for m in at.markdown)
    assert any("AI가 읽은 자료 본문" in e.label for e in at.expander)

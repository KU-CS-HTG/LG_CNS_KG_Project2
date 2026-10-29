"""SYNTHETIC applicability/configuration/UI fixtures; no real benefit formulas."""
from copy import deepcopy
import json

import pytest
from streamlit.testing.v1 import AppTest

from src.generate import answer_override as ao, benefit_applicability as ap, benefit_tiers as bt, amount_rules as ar

SID = "S-TAX"
_REAL_RULE_FIND = ar.find
EVIDENCE = [{"title": "Synthetic source", "url": "https://example.invalid/source", "quote": "Synthetic reviewed relationship"}]


def configuration():
    c = {"schema_version": ap.VERSION, "program_id": SID, "groups": [{"id": "household", "label": "가구 유형",
         "options": [{"id": i, "label": label} for i, label in ap.OPTIONS]}], "requirements": [], "items": []}
    c["requirements"].append({"id": "common", "text": "COMMON_REQUIREMENT", "applicability": ap.scope(), "evidence": EVIDENCE})
    c["items"].append({"id": "common-support", "kind": "support", "label": "공통 안내", "text": "COMMON_GUIDANCE",
                       "requires": ["common"], "applicability": ap.scope(), "evidence": EVIDENCE})
    for i, _ in ap.OPTIONS:
        c["requirements"].append({"id": "income-" + i, "text": "INCOME_" + i.upper(), "applicability": ap.scope([i]), "evidence": EVIDENCE})
        for kind in ap.KINDS:
            c["items"].append({"id": kind + "-" + i, "kind": kind, "label": kind + " " + i,
                 "text": "ONLY_" + i.upper() + "::" + kind, "requires": ["common", "income-" + i],
                 "applicability": ap.scope([i]), "evidence": [{**EVIDENCE[0], "quote": "EVIDENCE_ONLY_" + i.upper() + "::"}],
                 **({"variant_id": "v-" + i} if kind == "calculation" else {})})
    return c


def rule():
    return {"service_ids": [SID], "calculator": "generic", "as_of": "2026-10-29", "status": "SYNTHETIC",
            "inputs": [{"id": "input-" + i, "label": "INPUT_" + i.upper(), "type": "int", "min": 0} for i, _ in ap.OPTIONS],
            "variants": [{"id": "v-" + i, "name": "VARIANT_" + i.upper(),
                          "eligibility": [{"type": "range", "input": "input-" + i, "min": 0, "max": 10, "label": "synthetic range"}],
                          "payout": {"type": "fixed", "amount_won": n * 1000}} for n, (i, _) in enumerate(ap.OPTIONS, 1)],
            "sources": [{"title": "LEAK_RULE_SOURCE", "quote": "LEAK_RULE_QUOTE"}],
            "conflicts": [], "open_issues": [], "general_track_note": ""}


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    import requests
    import httpx
    from src.generate import condition_extractor
    from src.index import vector_store, embedder
    monkeypatch.setattr(ao, "OVERRIDE_DIR", tmp_path / "overrides")
    ao._cache.clear()
    bt._cache.clear()
    def forbidden(*a, **kw):
        pytest.fail("Category evaluation must not call external preparation or storage")
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(condition_extractor, "_chat_json", forbidden)
    monkeypatch.setattr(vector_store, "get_service_chunks", forbidden)
    monkeypatch.setattr(embedder, "embed_texts", forbidden)
    monkeypatch.setattr(embedder, "embed_query", forbidden)
    monkeypatch.setattr(ap, "_read_service", forbidden)
    monkeypatch.setattr(ar, "find", lambda sid: rule() if sid == SID else None)


@pytest.mark.parametrize("selected", [i for i, _ in ap.OPTIONS])
def test_evaluator_filters_all_item_kinds_and_unchecked_means_pending(selected):
    c = configuration()
    result = ap.evaluate(c, {"household": selected})
    assert not result["errors"]
    assert len(result["requirements"]) == 2
    assert {i["id"] for i in result["requirements"]} == {"common", "income-" + selected}
    assert len(result["pending"]) == 5
    for other, _ in ap.OPTIONS:
        if other != selected:
            assert all(other in i["id"] for i in result["inapplicable"] if i["id"].endswith(other))
            assert not any(i["id"].endswith(other) for i in result["pending"])
    met = ap.evaluate(c, {"household": selected}, {"common", "income-" + selected})
    assert len(met["selected"]) == 4 and len(met["common"]) == 1 and not met["pending"]


@pytest.mark.parametrize("selection", [{}, {"household": None}, {"household": ["single", "dual_earner"]},
                                         {"household": "foreign"}, {"foreign": "single"}])
def test_unknown_or_conflicting_selection_has_no_personalized_content(selection):
    r = ap.evaluate(configuration(), selection)
    assert r["selection_required"] or r["errors"]
    assert not r["common"] and not r["selected"] and not r["pending"]


@pytest.mark.parametrize("mutation", ["scope", "group", "option", "requirement", "requirement_scope", "proof", "program", "duplicate"])
def test_invalid_mappings_are_not_promoted_to_common(mutation):
    c = configuration()
    item = c["items"][1]
    if mutation == "scope": item["applicability"] = []
    elif mutation == "group": item["applicability"]["group_id"] = []
    elif mutation == "option": item["applicability"]["option_ids"] = ["missing"]
    elif mutation == "requirement": item["requires"] = ["missing"]
    elif mutation == "requirement_scope": item["requires"] = ["income-dual_earner"]
    elif mutation == "proof": item["evidence"] = []
    elif mutation == "program": c["program_id"] = "OTHER"
    else: item["id"] = c["items"][0]["id"]
    assert ap.validate(c, SID)[0] is None


def test_missing_applicability_and_invalid_legacy_requires_stay_unresolved():
    c = configuration()
    del c["items"][0]["applicability"]
    assert ap.validate(c)[0]["items"][0]["applicability"] == {"kind": "unresolved"}
    out = ap.evaluate(c, {"household": "single"})
    assert out["unresolved"][0]["id"] == "common-support"
    clean = bt.clean({"tiers": [{"label": "invalid", "benefit": "not common", "requires": [99]}]}, "", 1)
    assert not bt.evaluate(clean["tiers"], set())["base"]
    assert bt.evaluate(clean["tiers"], set())["unresolved"]


def test_multiple_supported_options_and_stable_identity_on_reorder():
    c = configuration()
    c["items"].append({"id": "shared-note", "kind": "note", "label": "shared", "text": "SHARED_NOTE", "requires": [],
                        "applicability": ap.scope(["single", "single_earner"]), "evidence": EVIDENCE})
    assert "shared-note" in {i["id"] for i in ap.evaluate(c, {"household": "single"})["selected"]}
    assert "shared-note" in {i["id"] for i in ap.evaluate(c, {"household": "dual_earner"})["inapplicable"]}
    before = ap.evaluate(c, {"household": "single"}, {"common", "income-single"})
    c["requirements"].reverse(); c["groups"][0]["options"].reverse()
    after = ap.evaluate(c, {"household": "single"}, {"common", "income-single"})
    assert [i["id"] for i in before["selected"]] == [i["id"] for i in after["selected"]]
    assert ap.evaluate(c, {"household": "single"}, {"income-dual_earner"})["errors"]


def test_amount_calculation_only_runs_selected_branch_after_inputs_and_checks(monkeypatch):
    calls = []
    original = ar.generic_calculate
    monkeypatch.setattr(ar, "generic_calculate", lambda r, v: calls.append(r) or original(r, v))
    c = configuration(); selected = {"household": "single"}; checked = {"common", "income-single"}
    assert "output" not in ap.calculation(rule(), c, selected, checked, {})[0]
    assert not calls
    assert "output" not in ap.calculation(rule(), c, selected, set(), {"input-single": 2})[0]
    out = ap.calculation(rule(), c, selected, checked, {"input-single": 2})
    assert len(calls) == 1 and [v["id"] for v in calls[0]["variants"]] == ["v-single"]
    assert out[0]["output"]["scenarios"][0]["amount_won"] == 1000


def _script():
    import streamlit as st
    from app import main
    from src.common.schema import Recommendation, Citation
    st.session_state.setdefault("checks", {})
    st.session_state.setdefault("guides", {})
    for key in ("a", "b"):
        r = Recommendation("Synthetic program", "Synthetic agency", "확인 필요",
            "LEAK_SUMMARY_OTHER_TYPE", "기존 신청 링크", service_id="S-TAX", needs_check=["LEGACY_MIXED_REQUIREMENT"],
            benefit_detail=["ORIGINAL_ALL_TYPES"], citations=[Citation("Synthetic", "agency", "ORIGINAL_QUOTE", "https://example.invalid/service")])
        main._card(1, r, key)


def _open(monkeypatch):
    ao.save(SID, "reviewer", {"applicability": configuration(),
        "support_lines": [{"label": "legacy", "text": "LEAK_MANUAL_SUPPORT"}],
        "amounts": [{"label": "legacy amount", "text": "LEAK_MANUAL_AMOUNT"}], "notes": ["LEAK_MANUAL_NOTE"],
        "source": {"title": "legacy", "url": "https://example.invalid/source", "level": "공식", "quote": "LEAK_MANUAL_QUOTE"}})
    monkeypatch.setattr(bt, "compute", lambda *a, **k: pytest.fail("Grouped edits must not call the LLM preparation path"))
    return AppTest.from_function(_script, default_timeout=30).run()


def _choose(at, choice, card="a"):
    next(r for r in at.radio if r.key.startswith("category_" + card + "_") and r.key.endswith("_group_household")).set_value(choice).run()
    assert not at.exception


def _button(at, key):
    at.button(key=key).click().run()
    assert not at.exception


def _visible(at):
    # Full original content is deliberately retained in collapsed HTML <details>.
    return "\n".join(str(e.value) for kind in (at.text, at.caption, at.info, at.warning, at.markdown)
                     for e in kind if not (hasattr(e.proto, "allow_html") and e.proto.allow_html))


@pytest.mark.parametrize("choice", [i for i, _ in ap.OPTIONS])
def test_render_all_personalized_paths_have_no_other_type_leakage(monkeypatch, choice):
    at = _open(monkeypatch)
    assert not at.exception and "가구 유형을 선택" in _visible(at)
    assert not any(b.key == "category_show_a" for b in at.button)
    _choose(at, choice)
    checkboxes = [c for c in at.checkbox if c.key.startswith("category_a_")]
    assert len(checkboxes) == 2 and {c.label for c in checkboxes} == {"COMMON_REQUIREMENT", "INCOME_" + choice.upper()}
    _button(at, "category_confirm_a")
    _button(at, "category_show_a")
    text = _visible(at)
    assert "COMMON_GUIDANCE" in text and "ONLY_" + choice.upper() + "::support" in text
    assert "남은 확인" in text and "예상 수령액:" not in text
    assert "해당 어려움" not in text
    for other, _ in ap.OPTIONS:
        if other != choice:
            assert "ONLY_" + other.upper() + "::" not in text and "EVIDENCE_ONLY_" + other.upper() + "::" not in text
    for fragment in ["LEAK_", "LEGACY_MIXED", "추가로 충족하면", "조건을 더 충족하면"]:
        assert fragment not in text
    assert not any(b.key == "category_show_b" for b in at.button)
    assert "원문 확인하기" in " ".join(m.value for m in at.markdown)


def test_type_checkbox_input_changes_require_new_confirmation_and_isolate_cards(monkeypatch):
    at = _open(monkeypatch); _choose(at, "single")
    for checkbox in list(at.checkbox):
        if checkbox.key.startswith("category_a_"):
            at.checkbox(key=checkbox.key).check().run()
    at.number_input[0].set_value(2).run()
    _button(at, "category_confirm_a"); _button(at, "category_show_a")
    assert "예상 수령액: 1천 원" in _visible(at)
    common_key = next(c.key for c in at.checkbox if c.key.endswith("_requirement_common"))
    at.checkbox(key=common_key).uncheck().run()
    assert "예상 수령액:" not in _visible(at) and "ONLY_SINGLE::support" not in _visible(at)
    at.checkbox(key=common_key).check().run()
    _button(at, "category_confirm_a"); _button(at, "category_show_a")
    at.number_input[0].set_value(3).run()
    assert "예상 수령액:" not in _visible(at)
    _button(at, "category_confirm_a"); _button(at, "category_show_a")
    _choose(at, "dual_earner")
    assert "ONLY_SINGLE::support" not in _visible(at) and "예상 수령액:" not in _visible(at)
    assert at.number_input[0].value is None
    assert at.checkbox(key=next(c.key for c in at.checkbox if c.key.endswith("_requirement_common"))).value is True
    assert next(c for c in at.checkbox if c.key.endswith("_requirement_income-dual_earner")).value is False
    assert "1개 확인" in _visible(at)
    _choose(at, "single_earner", "b")
    assert next(c for c in at.checkbox if c.key.startswith("category_b_") and c.key.endswith("_requirement_common")).value is False
    assert not any(b.key == "category_show_b" for b in at.button)


def test_config_revision_and_legacy_session_invalidate_results(monkeypatch):
    at = _open(monkeypatch); _choose(at, "single")
    _button(at, "category_confirm_a"); _button(at, "category_show_a")
    old = ao.find(SID)
    c = deepcopy(old["applicability"]); c["requirements"].reverse(); c["groups"][0]["options"].reverse()
    ao.save(SID, "reviewer", {**old, "applicability": c})
    at.session_state["checks_done_a"] = frozenset({1, 2, 3})
    at.session_state["tiers_open_a"] = True
    at.session_state["tiers_cache"] = {(SID, ()): {"tiers": [{"benefit": "LEAK_OLD_CACHE"}]}}
    at.run()
    assert not at.exception and "ONLY_SINGLE::support" not in _visible(at) and "LEAK_" not in _visible(at)
    assert "가구 유형을 선택" in _visible(at)


def test_invalid_config_has_no_generation_or_manual_fallback(monkeypatch):
    at = _open(monkeypatch)
    original = ao.find
    bad = {**original(SID), "applicability": {"program_id": SID}}
    monkeypatch.setattr(ao, "find", lambda sid: bad if sid == SID else None)
    at.run()
    assert not at.exception and any("유형 설정을 검수" in w.value for w in at.warning)
    assert "LEAK_" not in _visible(at) and "ONLY_" not in _visible(at)


def test_reviewed_configuration_roundtrip_ai_boundary_and_no_history_rewrite(tmp_path):
    folder = tmp_path / "events"
    c = configuration()
    ao.save("OTHER", "reviewer", {"benefit_text": "unrelated"}, folder)
    other_before = (folder / "reviewer.jsonl").read_bytes()
    ao.save(SID, "ai", {"applicability": c}, folder, ai=True)
    assert ao.overrides(folder)[SID]["applicability"] == c
    assert ao.pending_ai(ao.overrides(folder)[SID])
    ao.confirm(SID, "reviewer", folder)
    assert not ao.pending_ai(ao.overrides(folder)[SID])
    assert (folder / "reviewer.jsonl").read_bytes().startswith(other_before)
    with pytest.raises(ValueError): ao.save("OTHER", "reviewer", {"applicability": c}, folder)
    c["items"][0]["requires"] = ["foreign"]
    before = (folder / "reviewer.jsonl").read_bytes()
    with pytest.raises(ValueError): ao.save(SID, "reviewer", {"applicability": c}, folder)
    assert (folder / "reviewer.jsonl").read_bytes() == before


def test_ai_mapping_not_published_by_loading_or_rendering(monkeypatch):
    ao.save(SID, "ai", {"applicability": configuration()}, ai=True)
    assert ao.find(SID) is None and ap.config_for(SID, ao.find(SID)) is None
    assert ao.find_any(SID)["applicability"] and ao.pending_ai(ao.find_any(SID))


def test_corrupted_imported_ai_mapping_cannot_be_confirmed(tmp_path):
    folder = tmp_path / "events"
    ao.save(SID, "ai", {"applicability": configuration()}, folder, ai=True)
    path = folder / "ai.jsonl"
    event = json.loads(path.read_text(encoding="utf-8"))
    event["data"]["applicability"]["items"][0]["requires"] = ["foreign"]
    path.write_text(json.dumps(event), encoding="utf-8")
    with pytest.raises(ValueError):
        ao.confirm(SID, "reviewer", folder)
    assert not (folder / "reviewer.jsonl").exists() and ao.pending_ai(ao.overrides(folder)[SID])


def test_preparation_cache_includes_source_revision(monkeypatch):
    from src.common.schema import Chunk
    from src.generate import condition_extractor
    calls = []
    monkeypatch.setattr(condition_extractor, "_chat_json", lambda *a: calls.append(a) or {"tiers": [{"label": "guide", "benefit": "source", "requires": []}]})
    one = [Chunk("S#지원내용", "S", "test", "지원내용", "test\nsource A")]
    bt.compute("S", "test", [], one); bt.compute("S", "test", [], one)
    two = [Chunk("S#지원내용", "S", "test", "지원내용", "test\nsource B")]
    bt.compute("S", "test", [], two)
    assert len(calls) == 2


def test_rule_change_and_source_change_invalidate_confirmation(monkeypatch):
    at = _open(monkeypatch); _choose(at, "single")
    _button(at, "category_confirm_a"); _button(at, "category_show_a")
    changed = rule(); changed["status"] = "revised source"
    monkeypatch.setattr(ar, "find", lambda sid: changed)
    at.run()
    assert "ONLY_SINGLE::support" not in _visible(at) and "가구 유형을 선택" in _visible(at)


def _editor_script():
    import streamlit as st
    from app import category_editor
    from src.generate import answer_override
    saved = answer_override.find_any("S-TAX") or {}
    raw = {"support_lines": [{"label": "Legacy", "text": "LEGACY_UNCLASSIFIED"}]}
    draft = category_editor.render("S-TAX", {"service_id": "S-TAX"}, saved, raw)
    st.session_state["draft_config"] = draft


def test_admin_scopes_are_explicit_and_preview_does_not_publish():
    ao.save(SID, "ai", {"applicability": configuration()}, ai=True)
    before = (ao.OVERRIDE_DIR / "ai.jsonl").read_bytes()
    at = AppTest.from_function(_editor_script, default_timeout=30).run()
    assert not at.exception and ao.find(SID) is None
    draft = at.session_state["draft_config"]
    legacy = next(i for i in draft["items"] if i["text"] == "LEGACY_UNCLASSIFIED")
    assert legacy["applicability"] == {"kind": "unresolved"}
    key = f"category_editor_{SID}_{legacy['id']}_scope"
    assert at.selectbox(key=key).options == ["적용 유형 검수 필요", "공통 적용", "지정 유형"]
    at.selectbox(key=key).set_value("지정 유형").run()
    assert at.session_state["draft_config"]["items"][-1]["applicability"]["option_ids"] == []
    assert ap.validate(at.session_state["draft_config"])[0] is None
    assert ao.find(SID) is None and (ao.OVERRIDE_DIR / "ai.jsonl").read_bytes() == before


def test_confirmation_is_bound_to_card_selection_checks_inputs_and_revision():
    c = configuration(); sel = {"household": "single"}
    original = ap.confirmation(c, "a", sel, {"common"}, {"input-single": 1})
    assert original != ap.confirmation(c, "b", sel, {"common"}, {"input-single": 1})
    assert original != ap.confirmation(c, "a", {"household": "dual_earner"}, {"common"}, {"input-single": 1})
    assert original != ap.confirmation(c, "a", sel, set(), {"input-single": 1})
    assert original != ap.confirmation(c, "a", sel, {"common"}, {"input-single": 2})
    c["source_revision"] = "changed"
    assert original != ap.confirmation(c, "a", sel, {"common"}, {"input-single": 1})


@pytest.mark.parametrize("value", [None, True, "1", -1, float("nan"), float("inf")])
def test_incomplete_or_invalid_inputs_never_become_a_maximum_payment(value):
    assert "output" not in ap.calculation(rule(), configuration(), {"household": "single"},
        {"common", "income-single"}, {"input-single": value})[0]


def test_source_adapter_is_not_program_name_or_partial_word_classification():
    assert ap.config_for("UNRELATED", service={"service_id": "UNRELATED", "service_name": "단독 가구 근로장려금"}) is None
    profile = ap.config_for(ap.PROGRAM, service={"service_id": ap.PROGRAM, "support_content": "단독가구 99원\n홑벌이 가구 88원"})
    assert not profile["items"]
    assert ap.validate(profile)[0] is not None


def test_unclassified_rule_exceptions_block_estimate_instead_of_disappearing(monkeypatch):
    changed = rule()
    changed["open_issues"] = ["LEAK_OTHER_TYPE_EXCEPTION"]
    monkeypatch.setattr(ar, "find", lambda sid: changed)
    at = _open(monkeypatch); _choose(at, "single")
    for checkbox in list(at.checkbox):
        at.checkbox(key=checkbox.key).check().run()
    at.number_input[0].set_value(2).run()
    _button(at, "category_confirm_a"); _button(at, "category_show_a")
    assert "예상 수령액:" not in _visible(at)
    assert "주의사항·예외 적용 범위" in _visible(at) and "LEAK_OTHER_TYPE_EXCEPTION" not in _visible(at)
    # Explicit source-backed classification retains the necessary common note.
    c = configuration()
    c["items"].append({"id": "rule-exception", "kind": "note", "label": "예외", "text": changed["open_issues"][0],
                       "requires": [], "applicability": ap.scope(), "evidence": EVIDENCE})
    assert "output" in ap.calculation(changed, c, {"household": "single"}, {"common", "income-single"}, {"input-single": 2})[0]


def source_fixture():
    """Synthetic source layout only; the symbolic amounts are not real benefits."""
    return {"service_id": ap.PROGRAM, "service_name": "Synthetic tax source", "detail_url": "https://example.invalid/program",
            "selection_criteria": "○ 아래 요건을 모두 충족하는 합성 소득이 있는 가구\n(근로장려금)\n"
                "ㆍ 단독가구 : SINGLE_THRESHOLD\nㆍ 홑벌이 가구 : EARNER_THRESHOLD\nㆍ 맞벌이 가구 : DUAL_THRESHOLD\n"
                "(자녀장려금)\nㆍ CHILD_THRESHOLD\n- 재산요건\nㆍ 전년도 6월 1일 현재 SYNTHETIC_PROPERTY\n- 가구요건\n"
                "ㆍ 단독가구 : SINGLE_DEFINITION\nㆍ 홑벌이가구 : EARNER_DEFINITION\nㆍ 맞벌이가구 : DUAL_DEFINITION\n"
                "1) 배우자 : SPOUSE_DEFINITION\n2) 부양자녀 : DEPENDENT_DEFINITION\n<신청 제외> COMMON_EXCLUSION\n"
                "※ 총소득과 총급여액 등 비교\nCOMMON_INCOME_DEFINITION\n* 자세한 신청요건 참조",
            "support_content": "○ 전년도 연간 부부합산 총 급여액 등(합성 소득)에 따라\n- 근로장려금은\n"
                "ㆍ 단독가구 SINGLE_SUPPORT\nㆍ 홑벌이 가구 EARNER_SUPPORT\nㆍ 맞벌이 가구 DUAL_SUPPORT\n- 자녀 장려금은\n"
                "ㆍ 단독가구 해당 없음\nㆍ 홑벌이 가구 CHILD_EARNER_SUPPORT\nㆍ 맞벌이 가구 CHILD_DUAL_SUPPORT\n"
                "* 자세한 산정식은 SYNTHETIC_SOURCE"}


def test_source_relationships_keep_common_exceptions_component_conditions_and_exact_quotes():
    source = source_fixture()
    c = ap.config_for(ap.PROGRAM, service=source)
    assert ap.validate(c, ap.PROGRAM)[1] == []
    out = ap.evaluate(c, {"household": "single"})
    assert {r["id"] for r in out["requirements"]} == {"property", "exclusions", "income-source", "income-single"}
    assert {i["id"] for i in out["pending"]} == {"earned-single"}
    assert "income-definition" in {i["id"] for i in out["common"]}
    other = ap.evaluate(c, {"household": "single_earner"})
    assert "child-dependent-single_earner" in {r["id"] for r in other["requirements"]}
    text = source["selection_criteria"] + "\n" + source["support_content"]
    assert all(e["quote"] in text for row in c["requirements"] + c["items"] for e in row["evidence"])
    source["selection_criteria"] = source["selection_criteria"].replace("○ 아래 요건을 모두 충족하는 합성 소득이 있는 가구", "")
    incomplete = ap.config_for(ap.PROGRAM, service=source)
    assert not any(i["id"].startswith("earned-") for i in incomplete["items"])


def test_explicit_unknown_scope_does_not_require_fake_source_or_turn_common():
    c = configuration(); c["items"][0]["applicability"] = {"kind": "unresolved"}; c["items"][0]["evidence"] = []
    out = ap.evaluate(c, {"household": "single"})
    assert not out["errors"] and out["unresolved"] and not out["common"]


def test_rule_cache_uses_path_and_content_even_with_identical_timestamps(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(ar, "find", _REAL_RULE_FIND)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"rules": [{"service_ids": ["CACHE-SYN"], "name": "first"}]}), encoding="utf-8")
    b.write_text(json.dumps({"rules": []}), encoding="utf-8")
    for p in (a, b): os.utime(p, ns=(2_000_000_000, 2_000_000_000))
    ar._load.cache_clear()
    monkeypatch.setattr(ar, "RULES_PATH", a)
    assert ar.find("CACHE-SYN")["name"] == "first"
    monkeypatch.setattr(ar, "RULES_PATH", b)
    assert ar.find("CACHE-SYN") is None
    a.write_text(json.dumps({"rules": [{"service_ids": ["CACHE-SYN"], "name": "second"}]}), encoding="utf-8")
    os.utime(a, ns=(2_000_000_000, 2_000_000_000))
    monkeypatch.setattr(ar, "RULES_PATH", a)
    assert ar.find("CACHE-SYN")["name"] == "second"


def test_mid_render_configuration_change_cannot_use_old_confirmation(monkeypatch):
    at = _open(monkeypatch); _choose(at, "single")
    _button(at, "category_confirm_a"); _button(at, "category_show_a")
    original = ap.config_for
    calls = []
    def changing(*args, **kwargs):
        c = original(*args, **kwargs)
        calls.append(c)
        if len(calls) % 2 == 0:
            c = {**c, "source_revision": "changed between checklist and result"}
        return c
    monkeypatch.setattr(ap, "config_for", changing)
    at.run()
    assert not at.exception and "ONLY_SINGLE::support" not in _visible(at)
    assert "원문 또는 설정이 바뀌었어요" in _visible(at)

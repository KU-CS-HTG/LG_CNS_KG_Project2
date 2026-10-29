"""Integration contract: W5 scopes coexist with PR55 legacy display conditions."""
from src.generate import answer_override as ao
from tests.test_benefit_applicability import configuration, SID


def test_category_mapping_roundtrip_keeps_legacy_when_and_apply_url(tmp_path):
    config = configuration()
    raw = {
        "applicability": config,
        "apply_url": "https://example.invalid/apply",
        "needs_check": ["legacy one", "legacy two"],
        "support_lines": [{"label": "legacy", "text": "line one\nline two", "when": [1, 2]}],
    }
    ao.save(SID, "reviewer", raw, tmp_path)
    stored = ao.overrides(tmp_path)[SID]
    assert stored["applicability"] == config
    assert stored["support_lines"] == raw["support_lines"]
    assert stored["apply_url"] == raw["apply_url"]


def test_ai_confirmation_preserves_both_contracts(tmp_path):
    raw = {"applicability": configuration(), "apply_url": "https://example.invalid/apply",
           "needs_check": ["legacy"], "support_lines": [{"label": "legacy", "text": "legacy content", "when": [1]}]}
    ao.save(SID, "assistant", raw, tmp_path, ai=True)
    assert ao.pending_ai(ao.overrides(tmp_path)[SID])
    ao.confirm(SID, "reviewer", tmp_path)
    stored = ao.overrides(tmp_path)[SID]
    assert not ao.pending_ai(stored)
    assert stored["applicability"] == raw["applicability"]
    assert stored["support_lines"][0]["when"] == [1]
    assert stored["apply_url"] == raw["apply_url"]

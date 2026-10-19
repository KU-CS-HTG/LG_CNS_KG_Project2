"""Second-pass regressions: freshness, fixture isolation, and HTML boundaries."""
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.common import config
from src.ingest import client, pipeline, store
from src.ingest.normalize import clean_text
from src.ingest.merge import merge_fields
from src.ingest.storage import StoragePaths, resolve_storage

FIXTURE = Path("tests/fixtures/a_synthetic_api.json")
TODAY = "2026-10-19"


def test_deadline_extension_with_failed_detail_reproduction():
    old_detail = {"서비스ID": "A", "서비스명": "과거 청년 지원", "소관기관명": "과거 기관",
                  "신청기한": "2026-10-18", "지원대상": "청년", "지원내용": "과거 정상 본문"}
    old = store.clean_service(old_detail)
    old["_sources"] = {"serviceDetail": old_detail}
    run_id = client.new_run_id()
    store.start_run(run_id)
    snapshot = client.Collection("serviceList", [{"서비스ID": "A"}])
    store.apply_run(run_id, [old], snapshot, full=False, today=TODAY, summary={"status": "complete"})
    listing = {"서비스ID": "A", "서비스명": "현재 청년 지원", "소관기관명": "현재 기관", "신청기한": "2027-12-31"}
    services, _ = pipeline._join({
        "serviceList": client.Collection("serviceList", [listing]),
        "serviceDetail": client.Collection("serviceDetail", status="failed"),
        "supportConditions": client.Collection("supportConditions", [{"서비스ID": "A"}]),
    })
    assert services[0]["deadline"] == "2027-12-31"
    assert services[0]["service_name"] == "현재 청년 지원"
    assert services[0]["agency"] == "현재 기관"
    assert services[0]["support_content"] == "과거 정상 본문"


def test_fixture_default_does_not_write_live_destinations_reproduction():
    assert not config.DB_PATH.exists() and not config.RAW_DIR.exists()
    assert pipeline.main(["--offline-fixture", str(FIXTURE)]) == 0
    assert not config.DB_PATH.exists()
    assert not config.RAW_DIR.exists()


def test_html_numeric_cells_do_not_concatenate_reproduction():
    text = clean_text("<tr><td>19</td><td>34</td></tr>")
    assert "1934" not in text
    assert text.split() == ["19", "34"]


def test_deadline_correction_reopens_and_replaces_pending_idempotently(monkeypatch, a_storage):
    monkeypatch.setattr(pipeline, "seoul_today", lambda: TODAY)
    old_detail = {"서비스ID": "A", "서비스명": "과거 청년", "소관기관명": "과거 기관",
                  "서비스분야": "과거 분야", "상세조회URL": "https://www.gov.kr/old",
                  "신청기한": "2026-10-18", "지원대상": "청년", "지원내용": "과거 정상 본문"}
    old = store.clean_service(old_detail)
    old["_sources"] = {"serviceDetail": old_detail, "serviceList": {"서비스ID": "A"}}

    def apply_on(services, today):
        run_id = client.new_run_id()
        store.start_run(run_id, storage=a_storage)
        store.apply_run(run_id, services, client.Collection("serviceList"), full=False,
                        today=today, summary={"status": "partial"}, storage=a_storage)

    apply_on([old], "2026-10-17")
    apply_on([], TODAY)
    before = store.get_record("A", storage=a_storage)
    expired_event = store.get_pending_changes(storage=a_storage)[0]
    assert before["reason"] == "expired" and expired_event["action"] == "remove"
    current = {"서비스ID": "A", "서비스명": "현재 청년", "소관기관명": "현재 기관",
               "서비스분야": "현재 분야", "상세조회URL": "https://www.gov.kr/current", "신청기한": "2027-12-31"}

    def fetch(endpoint, **params):
        if endpoint == "serviceDetail":
            raise client.CollectionError("http_503")
        rows = [current] if endpoint == "serviceList" else [{"서비스ID": "A"}]
        return {"data": rows, "page": 1, "perPage": 1, "currentCount": 1, "totalCount": 1}

    chunks, summary = pipeline._execute(fetch_page=fetch, per_page=1, storage=a_storage)
    after = store.get_record("A", storage=a_storage)
    event = store.get_pending_changes(storage=a_storage)[0]
    assert after["status"] == "active" and after["reason"] is None
    assert after["content_hash"] != before["content_hash"]
    assert event["version"] == expired_event["version"] + 1 and event["action"] == "replace"
    assert store.acknowledge_changes([expired_event], storage=a_storage) == 0
    payload = after["payload"]
    assert payload["deadline"] == "2027-12-31" and payload["support_content"] == "과거 정상 본문"
    assert payload["service_name"] == "현재 청년" and payload["agency"] == "현재 기관"
    assert payload["field"] == "현재 분야" and payload["detail_url"] == "https://www.gov.kr/current"
    conflict = next(c for c in payload["_merge_conflicts"] if c["field"] == "application_period")
    assert conflict["source"] == "serviceList" and conflict["observation"] == "fresh_value"
    assert conflict["chosen_value"] == "2027-12-31" and conflict["reason"]
    assert any(c["observation"] == "retained_value" and c["source"] == "serviceDetail" for c in conflict["candidates"])
    assert summary["diagnostics"]["field_conflict_count"] >= 5
    saved_conflicts = json.loads((Path(summary["raw_dir"]) / summary["run_id"] / "merge-conflicts.json").read_text(encoding="utf-8"))
    assert any(c["field"] == "application_period" for c in saved_conflicts)
    assert all(c.metadata["deadline"] == "2027-12-31" for c in chunks)
    again, repeated = pipeline._execute(fetch_page=fetch, per_page=1, storage=a_storage)
    assert repeated["changed"] == 0 and repeated["unchanged"] == 1
    assert again == chunks and store.get_pending_changes(storage=a_storage) == [event]


@pytest.mark.parametrize("source_key, target", [("신청기한", "application_period"), ("서비스명", "service_name"),
    ("소관기관명", "agency"), ("상세조회URL", "detail_url"), ("서비스분야", "field"), ("신청방법", "how_to_apply")])
def test_missing_current_fields_fall_back_but_observed_empty_does_not(source_key, target):
    value = "https://www.gov.kr/old" if target == "detail_url" else "기존 값"
    previous = {"serviceDetail": {source_key: value}}
    merged, provenance, conflicts = merge_fields({"서비스ID": "A"}, None, previous)
    assert merged[source_key] == value
    assert provenance[target]["observation"] == "retained_value"
    for empty in (None, "", " \n "):
        result = merge_fields({"서비스ID": "A", source_key: empty}, None, previous)
        assert result == merge_fields({"서비스ID": "A", source_key: empty}, None, previous)
        merged, provenance, conflicts = result
        assert clean_text(merged[source_key]) == "" and provenance[target]["observation"] == "fresh_empty"
        assert any(c["field"] == target and c["chosen_value"] == "" for c in conflicts)


def test_previous_listing_fallback_and_current_empty_agency_quality():
    previous = {"serviceList": {"서비스명": "청년", "소관기관명": "기관", "지원내용": "보존"}}
    merged, provenance, _ = merge_fields({"서비스ID": "A"}, None, previous)
    assert provenance["agency"]["source"] == "serviceList" and merged["소관기관명"] == "기관"
    merged, _, _ = merge_fields({"서비스ID": "A", "소관기관명": None}, None, previous)
    service = store.clean_service(merged)
    assert "missing_agency" in service["data_quality"] and "agency" not in store.build_metadata(service)


def test_successful_missing_field_and_unavailable_response_have_distinct_provenance():
    _, provenance, _ = merge_fields({"서비스ID": "A"}, None, {"serviceDetail": {"소관기관명": "기존 기관"}})
    inputs = provenance["agency"]["inputs"]
    assert inputs[0] == {"source": "serviceList", "observation": "fresh_missing"}
    assert inputs[1] == {"source": "serviceDetail", "observation": "unavailable"}
    assert inputs[2] == {"source": "serviceDetail", "observation": "retained_value"}
    assert provenance["agency"]["chosen_value"] == "기존 기관"


def test_merge_does_not_parse_escaped_literal_markup_twice():
    row = {"서비스ID": "A", "서비스명": "청년", "지원대상": "&lt;b&gt;19세 이하 제외&lt;/b&gt;"}
    merged, _, _ = merge_fields(row, row, {})
    assert store.clean_service(merged)["support_target"] == store.clean_service(row)["support_target"]
    assert "19세 이하 제외" in store.clean_service(merged)["support_target"]


def test_resolved_listing_value_and_explicit_removal_survive_later_missing_keys(monkeypatch, a_storage):
    monkeypatch.setattr(pipeline, "seoul_today", lambda: TODAY)
    historic = {"서비스ID": "A", "서비스명": "청년", "지원대상": "청년", "소관기관명": "과거 기관", "신청기한": "2026-10-18"}
    old = store.clean_service(historic)
    old["_sources"] = {"serviceDetail": historic}
    store.upsert_services([old], storage=a_storage)
    current = {"서비스ID": "A", "서비스명": "청년", "신청기한": "2027-12-31", "소관기관명": ""}

    def fetch(endpoint, **params):
        if endpoint == "serviceDetail":
            raise client.CollectionError("http_503")
        rows = [dict(current)] if endpoint == "serviceList" else [{"서비스ID": "A"}]
        return {"data": rows, "page": 1, "perPage": 1, "currentCount": 1, "totalCount": 1}

    pipeline._execute(fetch_page=fetch, per_page=1, storage=a_storage)
    event = store.get_pending_changes(storage=a_storage)[0]
    current.clear()
    current["서비스ID"] = "A"
    for _ in range(2):
        _, summary = pipeline._execute(fetch_page=fetch, per_page=1, storage=a_storage)
        value = store.get_record("A", storage=a_storage)["payload"]
        assert value["deadline"] == "2027-12-31" and value["agency"] == ""
        assert value["service_name"] == "청년" and value["support_target"] == "청년"
        assert summary["changed"] == 0
        assert store.get_pending_changes(storage=a_storage) == [event]


@pytest.mark.parametrize("listing_period, detail_period, chosen, reason", [
    ("2027-12-31", "2026-12-31", "2026-12-31", "earlier_end"),
    ("2026-12-31", "2027-12-31", "2026-12-31", "earlier_end"),
    ("2027-12-31", "별도 공고", "별도 공고", "ambiguous"),
])
def test_two_current_sources_have_explicit_conservative_deadline_rule(listing_period, detail_period, chosen, reason):
    listing = {"서비스ID": "A", "서비스명": "목록 청년", "신청기한": listing_period}
    detail = {"서비스ID": "A", "서비스명": "상세 청년", "신청기한": detail_period, "수정일시": "2099-01-01"}
    result = merge_fields(listing, detail, {})
    merged, provenance, conflicts = result
    assert merged["신청기한"] == chosen and merged["서비스명"] == "목록 청년"
    assert reason in provenance["application_period"]["reason"]
    assert result == merge_fields(listing, detail, {})
    assert all(c["requires_review"] for c in conflicts)


def test_current_empty_name_is_deferred_and_conflict_audited():
    old = store.clean_service({"서비스ID": "A", "서비스명": "과거 청년"})
    old["_sources"] = {"serviceDetail": {"서비스명": "과거 청년"}}
    store.upsert_services([old])
    services, diagnostics = pipeline._join({
        "serviceList": client.Collection("serviceList", [{"서비스ID": "A", "서비스명": ""}]),
        "serviceDetail": client.Collection("serviceDetail"), "supportConditions": client.Collection("supportConditions")})
    assert services == [] and diagnostics["invalid_records"] == 1
    assert any(c["field"] == "service_name" and c["observation"] == "fresh_empty" for c in diagnostics["field_conflicts"])
    assert store.load_services()[0]["service_name"] == "과거 청년"


def test_fixture_pending_state_and_live_data_are_separate(tmp_path):
    live = store.clean_service({"서비스ID": "LIVE-A", "서비스명": "청년 운영 데이터"})
    store.upsert_services([live])
    before = config.DB_PATH.read_bytes()
    live_pending = store.get_pending_changes()
    configured = (config.DB_PATH, config.RAW_DIR)
    chunks, summary = pipeline.run_fixture(FIXTURE)
    assert summary["mode"] == "offline" and chunks
    isolated = StoragePaths(Path(summary["db_path"]), Path(summary["raw_dir"]), "offline")
    assert store.get_pending_changes(storage=isolated) and config.DB_PATH.read_bytes() == before
    assert (config.DB_PATH, config.RAW_DIR) == configured
    assert store.get_pending_changes() == live_pending
    assert not config.RAW_DIR.exists()
    assert store.acknowledge_changes(store.get_pending_changes(storage=isolated), storage=isolated) == 2
    assert store.get_pending_changes() == live_pending
    assert all(not e["can_close_missing"] for e in summary["endpoints"].values())


@pytest.mark.parametrize("destination", ["db", "raw", "db_alias", "raw_alias", "raw_child", "raw_parent", "wal", "shm", "lock"])
def test_fixture_live_destination_rejected_before_any_write(tmp_path, monkeypatch, destination):
    db_path, raw_dir = tmp_path / "offline" / "services.db", tmp_path / "offline" / "raw"
    if destination == "db": db_path = config.DB_PATH
    elif destination == "raw": raw_dir = config.RAW_DIR
    elif destination == "db_alias": db_path = config.DB_PATH.parent / "uncreated-alias" / ".." / config.DB_PATH.name
    elif destination == "raw_alias": raw_dir = config.RAW_DIR / "uncreated-alias" / ".."
    elif destination == "raw_child": raw_dir = config.RAW_DIR / "synthetic"
    elif destination == "raw_parent": raw_dir = config.RAW_DIR.parent
    else: db_path = Path(str(config.DB_PATH) + {"wal": "-wal", "shm": "-shm", "lock": ".ingest.lock"}[destination])
    mkdir, connect, write = Mock(side_effect=AssertionError("write before validation")), Mock(), Mock()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", mkdir)
        patch.setattr(store.sqlite3, "connect", connect)
        patch.setattr(client, "write_json", write)
        with pytest.raises(ValueError):
            pipeline.run_fixture(FIXTURE, db_path=db_path, raw_dir=raw_dir)
    assert mkdir.call_count == connect.call_count == write.call_count == 0
    assert not config.DB_PATH.exists() and not config.RAW_DIR.exists()


def test_existing_hardlink_to_live_db_rejected(tmp_path):
    config.DB_PATH.write_bytes(b"live sentinel")
    alias = tmp_path / "alias.db"
    os.link(config.DB_PATH, alias)
    with pytest.raises(ValueError, match="overlaps_live"):
        pipeline.run_fixture(FIXTURE, db_path=alias, raw_dir=tmp_path / "offline-raw")
    assert config.DB_PATH.read_bytes() == b"live sentinel"


def test_cli_rejects_live_alias_and_programmatic_legacy_source_is_isolated(capsys):
    assert pipeline.main(["--offline-fixture", str(FIXTURE), "--db-path", str(config.DB_PATH)]) == 1
    assert "overlaps_live" in capsys.readouterr().out
    assert not config.DB_PATH.exists()
    _, summary = pipeline._execute(fetch_page=pipeline._fixture_fetch(FIXTURE))
    assert Path(summary["db_path"]) != config.DB_PATH.resolve()
    assert Path(summary["raw_dir"]) != config.RAW_DIR.resolve()
    with pytest.raises(ValueError, match="overlaps_live"):
        pipeline._execute(fetch_page=pipeline._fixture_fetch(FIXTURE), storage=resolve_storage())


def test_live_defaults_remain_and_validation_requires_bounded_isolated_storage(monkeypatch):
    defaults = resolve_storage()
    assert defaults.mode == "live" and defaults.db_path == config.DB_PATH.resolve()
    assert defaults.raw_dir == config.RAW_DIR.resolve()
    with pytest.raises(ValueError, match="page_limit"):
        pipeline._execute(mode="validation")
    with pytest.raises(ValueError, match="overlaps_live"):
        pipeline._execute(mode="validation", max_pages=1, storage=defaults)
    assert not config.DB_PATH.exists()


@pytest.mark.parametrize("html_text, rows", [
    ("<table><tr><th>최소 연령</th><th>최대 연령</th></tr><tr><td>19</td><td>34</td></tr></table>", [["최소", "연령", "최대", "연령"], ["19", "34"]]),
    ("<tr><td>19</td><td>34</td></tr><tr><td>10만 원</td><td>중복 불가</td></tr>", [["19", "34"], ["10만", "원", "중복", "불가"]]),
    ("<table><tr><td><strong>19</strong><br>예외: <em>제외 아님</em></td><td><div>34</div><p>세 이하</p></td></tr></table>", [["19"], ["예외:", "제외", "아님"], ["34"], ["세", "이하"]]),
    ("<tr><th>소득 &amp; 조건</th><td>100&nbsp;% 이하 &#xCCAD;&#45380;</td></tr>", [["소득", "&", "조건", "100", "%", "이하", "청년"]]),
])
def test_html_boundaries_semantics_and_deterministic_chunks(html_text, rows):
    normalized = clean_text(html_text)
    lines = [line.split() for line in normalized.splitlines() if line.strip()]
    for row in rows:
        assert row in lines
    assert normalized == clean_text(html_text)
    raw = {"서비스ID": "A", "서비스명": "청년 표 지원", "지원대상": html_text}
    service = store.clean_service(raw)
    from src.ingest.chunker import chunk_service
    chunks = chunk_service(service, max_chars=25)
    assert "".join(c.text[len("청년 표 지원\n"):] for c in chunks) == normalized
    assert store.clean_service(raw)["content_hash"] == service["content_hash"]


@pytest.mark.parametrize("value, expected", [(None, ""), ("", ""), (" \n ", ""), (19, "19"),
    ("19~34세, 10만 원. 단, 중복 불가", "19~34세, 10만 원. 단, 중복 불가"),
    ("소득 < 50만 원 & 예외", "소득 < 50만 원 & 예외"), ("A\r\nB", "A\nB")])
def test_plain_text_and_missing_values_keep_meaning(value, expected):
    assert clean_text(value) == expected


def test_table_fix_triggers_one_time_hash_and_whole_service_replacement():
    raw = {"서비스ID": "A", "서비스명": "청년 표", "지원대상": "1934"}
    store.upsert_services([store.clean_service(raw)])
    before = store.get_pending_changes()[0]
    raw["지원대상"] = "<tr><td>19</td><td>34</td></tr>"
    corrected = store.clean_service(raw)
    assert store.upsert_services([corrected]) == ["A"]
    after = store.get_pending_changes()[0]
    assert after["action"] == "replace" and after["version"] == before["version"] + 1
    assert "1934" not in after["chunks"][0]["text"]
    assert store.acknowledge_changes([before]) == 0
    assert store.upsert_services([corrected]) == [] and store.get_pending_changes() == [after]

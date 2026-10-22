"""Transactional historical replay progress; no external services/models."""
import json
from pathlib import Path
import sqlite3

import pytest

from src.ingest import client, normalize, pipeline, policy, reprocess, store
from src.ingest.storage import StoragePaths

TODAY = "2026-10-22"


@pytest.fixture
def target(tmp_path):
    return StoragePaths(tmp_path / "target/state.db", tmp_path / "replay-output", "test")


def source_run(paths, monkeypatch, *, missing_detail=None):
    rows = [{"서비스ID": f"P{n}", "서비스명": f"가구 지원 {n}", "지원대상": "서울특별시 거주자",
             "지원내용": "생활 지원", "선정기준": "소득 조건", "신청방법": "온라인", "신청기한": "상시",
             "소관기관명": "부산광역시", "상세조회URL": "https://www.gov.kr/synthetic"} for n in range(5)]

    def fetch(endpoint, **params):
        data = [r for r in rows if r["서비스ID"] != missing_detail] if endpoint == "serviceDetail" else rows
        if endpoint == "supportConditions":
            data = [{"서비스ID": r["서비스ID"], "JA0110": 0} for r in reversed(rows)]
        return {"data": data, "page": 1, "perPage": 50, "currentCount": len(data), "totalCount": len(data)}

    with monkeypatch.context() as m:
        m.setattr(client, "new_run_id", lambda: "20261020T060000Z-source")
        m.setattr(pipeline, "seoul_today", lambda: TODAY)
        pipeline._execute(fetch_page=fetch, storage=paths, max_pages=1, per_page=50)
    return paths.raw_dir / "20261020T060000Z-source"


def test_consecutive_segments_resume_and_explicit_later_offset(a_storage, target, monkeypatch, tmp_path):
    root = source_run(a_storage, monkeypatch)
    first = reprocess.reprocess(root, storage=target, limit=2, today=TODAY, checkpoint="r")
    assert (first["attempted"], first["processed"], first["next_position"], first["end_of_source"]) == (2, 2, 2, False)
    assert first["replay_checkpoint"]["binding"]["target_db_id"]
    second = reprocess.reprocess(root, storage=target, limit=2, today=TODAY, checkpoint="r", resume=True)
    assert second["offset"] == 2 and second["next_position"] == 4
    third = reprocess.reprocess(root, storage=target, limit=2, today=TODAY, checkpoint="r", resume=True)
    assert third["offset"] == 4 and third["attempted"] == 1 and third["end_of_source"]
    assert {s["service_id"] for s in store.load_services(storage=target)} == {f"P{n}" for n in range(5)}
    token = store.get_pending_changes(storage=target)
    end = reprocess.reprocess(root, storage=target, limit=2, today=TODAY, checkpoint="r", resume=True)
    assert end["attempted"] == 0 and end["end_of_source"] and store.get_pending_changes(storage=target) == token
    later = StoragePaths(tmp_path / "later.db", tmp_path / "later-raw", "test")
    reprocess.reprocess(root, storage=later, offset=3, limit=1, today=TODAY)
    assert [s["service_id"] for s in store.load_services(storage=later)] == ["P3"]
    assert all(not endpoint["can_close_missing"] for endpoint in third["endpoints"].values())
    assert store.load_services(storage=target)[0]["_observations"]["serviceDetail"] == "historical"
    assert "fresh_" not in json.dumps(store.load_services(storage=target)[0]["eligibility"])


def test_transaction_failure_never_advances_progress(a_storage, target, monkeypatch):
    root = source_run(a_storage, monkeypatch)
    reprocess.reprocess(root, storage=target, limit=2, today=TODAY, checkpoint="r")
    tokens = store.get_pending_changes(storage=target)
    with monkeypatch.context() as m:
        m.setattr(store, "_enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected_transaction_failure")))
        with pytest.raises(RuntimeError, match="injected"):
            reprocess.reprocess(root, storage=target, limit=2, today=TODAY, checkpoint="r", resume=True)
    assert store.read_state("replay:r", storage=target)["next_position"] == 2
    assert store.get_record("P2", storage=target) is None
    assert store.get_pending_changes(storage=target) == tokens
    resumed = reprocess.reprocess(root, storage=target, limit=2, today=TODAY, checkpoint="r", resume=True)
    assert resumed["offset"] == 2 and resumed["next_position"] == 4


def test_auxiliary_output_failure_after_commit_is_recoverable(a_storage, target, monkeypatch):
    root = source_run(a_storage, monkeypatch)
    original = client.write_json

    def fail_summary(path, value):
        if path.name == "run-summary.json":
            raise OSError("auxiliary output unavailable")
        return original(path, value)

    with monkeypatch.context() as m:
        m.setattr(client, "write_json", fail_summary)
        with pytest.raises(OSError):
            reprocess.reprocess(root, storage=target, limit=2, today=TODAY, checkpoint="r")
    state = store.read_state("replay:r", storage=target)
    assert state["next_position"] == 2
    assert store.get_run(state["last_run"], storage=target)["status"] == "sample"
    resumed = reprocess.reprocess(root, storage=target, limit=2, today=TODAY, checkpoint="r", resume=True)
    assert resumed["offset"] == 2 and resumed["new"] == 2 and resumed["next_position"] == 4


@pytest.mark.parametrize("changed", ["source", "policy", "normalizer", "implementation", "reference_date", "order", "database_id", "target_path", "chunking"])
def test_changed_bindings_reject_resume(a_storage, target, monkeypatch, changed, tmp_path):
    root = source_run(a_storage, monkeypatch)
    reprocess.reprocess(root, storage=target, limit=1, today=TODAY, checkpoint="r")
    selected_target, reference = target, TODAY
    if changed == "source":
        path = root / "serviceList-00001.json"
        payload = json.loads(path.read_text(encoding="utf-8")); payload["audit"] = "changed content"
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif changed == "policy":
        monkeypatch.setattr(policy, "POLICY_ID", "other-policy")
    elif changed == "normalizer":
        monkeypatch.setattr(normalize, "NORMALIZATION_VERSION", "other-normalizer")
    elif changed == "implementation":
        monkeypatch.setattr(reprocess, "implementation_fingerprint", lambda: "other-implementation")
    elif changed == "reference_date":
        reference = "2026-10-23"
    elif changed == "order":
        monkeypatch.setattr(reprocess, "SOURCE_ORDER", "other-order")
    elif changed == "database_id":
        with store.connection(storage=target) as con:
            con.execute("UPDATE ingest_state SET value=? WHERE key='database_id'", (json.dumps("other-db"),))
    elif changed == "chunking":
        from src.common import config
        monkeypatch.setattr(config, "MAX_CHUNK_CHARS", 123)
    else:
        destination = tmp_path / "copied.db"
        with sqlite3.connect(target.db_path) as source:
            with sqlite3.connect(destination) as dest:
                source.backup(dest)
        selected_target = StoragePaths(destination, tmp_path / "copied-raw", "test")
    with pytest.raises(ValueError, match="binding_changed"):
        reprocess.reprocess(root, storage=selected_target, limit=1, today=reference, checkpoint="r", resume=True)
    assert store.read_state("replay:r", storage=target)["next_position"] == 1


def test_deferred_is_not_success_and_end_is_not_completeness(a_storage, target, monkeypatch):
    root = source_run(a_storage, monkeypatch, missing_detail="P1")
    result = reprocess.reprocess(root, storage=target, limit=5, today=TODAY, checkpoint="r")
    assert result["end_of_source"] and result["attempted"] == 5 and result["processed"] == 4 and result["deferred"] == 1
    state = store.read_state("replay:r", storage=target)
    assert state["deferred_remaining"]["P1"]["position"] == 1
    assert state["deferred_remaining"]["P1"]["reason"] == "historical_detail_unavailable_deferred"
    assert state["end_of_source"] and not result["endpoints"]["serviceList"]["can_close_missing"]
    with pytest.raises(ValueError, match="exists"):
        reprocess.reprocess(root, storage=target, checkpoint="r", today=TODAY)
    with pytest.raises(ValueError, match="beyond_source_end"):
        reprocess.reprocess(root, storage=target, offset=6, today=TODAY)


def test_segment_resume_cli_and_explicit_reference(a_storage, target, monkeypatch):
    root = source_run(a_storage, monkeypatch)
    base = ["--source-run", str(root), "--db-path", str(target.db_path), "--raw-dir", str(target.raw_dir),
            "--limit", "2", "--today", TODAY, "--validation"]
    assert reprocess.main([*base, "--offset", "1", "--checkpoint", "cli"]) == 0
    assert reprocess.main([*base, "--resume", "cli"]) == 0
    assert store.read_state("replay:cli", storage=target)["next_position"] == 5
    assert {s["service_id"] for s in store.load_services(storage=target)} == {"P1", "P2", "P3", "P4"}


def test_conflicting_ids_remain_identifiable_outside_validated_cursor(a_storage, target, monkeypatch):
    root = source_run(a_storage, monkeypatch)
    page_path = root / "serviceList-00001.json"
    page = json.loads(page_path.read_text(encoding="utf-8"))
    page["data"].append({**page["data"][0], "서비스명": "conflicting source"})
    page.update(currentCount=6, totalCount=6)
    page_path.write_text(json.dumps(page), encoding="utf-8")
    summary_path = root / "run-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    state = summary["endpoints"]["serviceList"]
    state.update(status="partial", count=4, total=6, conflicts=["P0"], diagnostics=["duplicate_id"])
    (root / "serviceList-summary.json").write_text(json.dumps(state), encoding="utf-8")
    summary["status"] = "partial"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    result = reprocess.reprocess(root, storage=target, limit=5, today=TODAY, checkpoint="r")
    assert result["total_source_records"] == 4 and result["end_of_source"]
    assert result["source_conflicting_ids"] == ["P0"]
    assert store.read_state("replay:r", storage=target)["deferred_remaining"]["P0"]["reason"] == "conflicting_source_listing_id"
    assert store.get_record("P0", storage=target) is None

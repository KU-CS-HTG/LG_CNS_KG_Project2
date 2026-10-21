"""Synthetic acquisition replay; real B/Chroma with fake embeddings only."""
import ast
from dataclasses import replace
import json
from pathlib import Path

import pytest

from src.ingest import client, pipeline, policy, reprocess, store, handoff, coverage
from src.ingest.handoff_index import BIndexAdapter
from src.ingest.storage import StoragePaths
from src.index import vector_store as index
from tests.ingest.test_store_pipeline import service

TODAY = "2026-10-21"


@pytest.mark.parametrize("name,target", [
    ("근로장려금", "저소득 근로 가구"), ("아동수당", "만 8세 미만 자녀를 둔 부모"),
    ("첫만남이용권", "출생 아동의 보호자"), ("임산부 진료", "임신 중인 주민"),
    ("사업 참여 지원", "개인 사업자 및 가구"), ("신혼 주택", "혼인 가구"),
    ("교육비", "학생"), ("돌봄 서비스", "돌봄이 필요한 가구")])
def test_general_and_dependent_programs_included(name, target):
    value = service(서비스명=name, 지원대상=target)
    assert policy.inclusion_decision(value)["outcome"] == "include"
    assert policy.is_included_service(value)
    assert "age_min" not in store.build_metadata(value)


def test_policy_exclusion_is_narrow_and_unknown_is_not_ineligible():
    assert policy.inclusion_decision(service(서비스명="시설 지원", 지원대상="공공기관만"))["outcome"] == "exclude"
    for target in ("사업 수행", "65세 이상", "", "법인만 또는 개인 가구도 가능", "법인만두 지원", "공공기관만 지원하는 것은 아님"):
        value = service(서비스명="일반 제도", 지원대상=target)
        assert policy.is_included_service(value)
    ambiguous = policy.inclusion_decision(service(서비스명="일반 제도", 지원대상=""))
    assert ambiguous["outcome"] == "provisional" and ambiguous["uncertainty"]
    dependent = store.clean_service({"서비스ID": "child", "서비스명": "양육수당"},
        {"_verified_eligibility": {"source": "dependent", "age_ranges": [[0, 8]]}})
    assert "age_min" not in store.build_metadata(dependent)
    assert policy.is_included_service(dependent)
    tree = ast.parse(Path(policy.__file__).read_text(encoding="utf-8"))
    assert not any(isinstance(n, (ast.Import, ast.ImportFrom)) and "eval" in ast.unparse(n) for n in ast.walk(tree))
    assert "q15" not in Path(policy.__file__).read_text(encoding="utf-8")


def acquire(paths, monkeypatch, *, sid="P", detail=True, conditions=True, period="상시"):
    raw = {"서비스ID": sid, "서비스명": "아동수당", "지원대상": "아동을 양육하는 가구", "소관기관명": "서울특별시",
           "상세조회URL": "https://www.gov.kr/synthetic/" + sid, "신청기한": period,
           "선정기준": "가구 조건 확인", "지원내용": "매월 지급", "신청방법": "온라인 신청"}

    def fetch(endpoint, **params):
        if (endpoint == "serviceDetail" and not detail) or (endpoint == "supportConditions" and not conditions):
            raise client.CollectionError("source_failed")
        rows = [raw] if endpoint != "supportConditions" else [{"서비스ID": sid, "JA0110": 0, "JA0111": 8}]
        return {"data": rows, "page": 1, "perPage": 50, "currentCount": 1, "totalCount": 1}

    with monkeypatch.context() as m:
        m.setattr(client, "new_run_id", lambda: "20261020T061626Z-source")
        m.setattr(pipeline, "seoul_today", lambda: TODAY)
        pipeline._execute(fetch_page=fetch, storage=paths, max_pages=1, per_page=50)
    return paths.raw_dir / "20261020T061626Z-source"


@pytest.fixture
def target(tmp_path):
    return StoragePaths(tmp_path / "target/services.db", tmp_path / "replay-output", "test")


def seed_legacy(paths, source_id, *, reason="youth_scope_exit"):
    value = service("P", 서비스명="아동수당", 지원대상="아동을 양육하는 가구", 선정기준="가구 조건 확인",
                    지원내용="매월 지급", 신청방법="온라인 신청", 소관기관명="서울특별시", 신청기한="상시")
    # Hash matches incoming evidence despite the old policy storing only a minimal audit.
    with store.connection(storage=paths) as con:
        con.execute("INSERT INTO services VALUES (?, ?, ?, 'inactive', ?, 7, ?)",
            ("P", json.dumps({"service_id": "P", "service_name": value["service_name"]}), value["content_hash"], reason, source_id))
    return value


def test_replay_policy_transition_same_hash_full_event_and_idempotence(a_storage, target, monkeypatch):
    source = acquire(a_storage, monkeypatch)
    value = seed_legacy(target, source.name)
    first = reprocess.reprocess(source, storage=target, today=TODAY)
    record = store.get_record("P", storage=target)
    event = store.get_pending_changes(storage=target)[0]
    assert record["status"] == "active" and record["version"] == 8
    assert record["content_hash"] == value["content_hash"]
    assert first["changed"] == 1 and event["action"] == "replace" and len(event["chunks"]) == 4
    assert record["payload"]["_observations"]["serviceDetail"] == "historical"
    assert record["payload"]["_acquisition"]["run_id"] == source.name
    assert "fresh_" not in json.dumps(record["payload"]["_field_provenance"])
    assert record["payload"]["inclusion_policy"]["policy_id"] == policy.POLICY_ID
    again = reprocess.reprocess(source, storage=target, today=TODAY)
    assert again["changed"] == 0 and again["unchanged"] == 1
    assert store.get_record("P", storage=target)["payload"]["_previous_scope_reason"] == "youth_scope_exit"
    assert store.get_pending_changes(storage=target) == [event]
    assert all(not e["can_close_missing"] for e in again["endpoints"].values())


def test_replay_cannot_overwrite_newer_or_unknown_or_disappeared(a_storage, target, monkeypatch):
    source = acquire(a_storage, monkeypatch)
    store.upsert_services([service("P", 신청기한="2027-12-31", 지원내용="newer text")], storage=target)
    before = store.get_record("P", storage=target)
    assert reprocess.reprocess(source, storage=target, today=TODAY)["replay_deferred"][0]["reason"] == "observation_order_unknown_deferred"
    with store.connection(storage=target) as con:
        p = before["payload"]
        p["_acquisition"] = {"run_id": "unknown", "acquired_at": "2026-10-19T00:00:00"}
        con.execute("UPDATE services SET payload=?", (json.dumps(p),))
    assert reprocess.reprocess(source, storage=target, today=TODAY)["replay_deferred"][0]["reason"] == "observation_order_unknown_deferred"
    with store.connection(storage=target) as con:
        p = before["payload"]
        p["_acquisition"] = {"run_id": "later", "acquired_at": "2026-10-21T00:00:00Z"}
        con.execute("UPDATE services SET payload=?", (json.dumps(p),))
    assert "older" in reprocess.reprocess(source, storage=target, today=TODAY)["replay_deferred"][0]["reason"]
    assert store.get_record("P", storage=target)["payload"]["support_content"] == "newer text"
    with store.connection(storage=target) as con:
        con.execute("UPDATE services SET status='inactive',reason='api_disappeared'")
    assert "disappearance" in reprocess.reprocess(source, storage=target, today=TODAY)["replay_deferred"][0]["reason"]


@pytest.mark.parametrize("detail,conditions,expected", [(False, True, "detail"), (True, False, "conditions")])
def test_missing_source_content_explicit_deferral(a_storage, target, monkeypatch, detail, conditions, expected):
    source = acquire(a_storage, monkeypatch, detail=detail, conditions=conditions)
    seed_legacy(target, source.name)
    result = reprocess.reprocess(source, storage=target, today=TODAY)
    assert result["deferred"] == 1 and expected in result["replay_deferred"][0]["reason"]
    assert not store.get_pending_changes(storage=target)
    assert store.get_record("P", storage=target)["status"] == "inactive"


def test_replay_never_closes_unrelated_and_respects_expiry(a_storage, target, monkeypatch):
    source = acquire(a_storage, monkeypatch, period="2020-01-01")
    store.upsert_services([service("unrelated")], storage=target)
    result = reprocess.reprocess(source, storage=target, today=TODAY)
    assert store.get_record("P", storage=target)["reason"] == "expired"
    assert store.get_record("unrelated", storage=target)["status"] == "active"
    assert result["endpoints"]["serviceList"]["can_close_missing"] is False


def test_invalid_manifest_and_label_helper_layout_rejected(a_storage, target, monkeypatch, tmp_path):
    cache = tmp_path / "label-cache"
    cache.mkdir()
    (cache / "serviceList.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="requires_A_run"):
        reprocess.reprocess(cache, storage=target)
    assert not target.db_path.exists()
    source = acquire(a_storage, monkeypatch)
    p = source / "serviceDetail-summary.json"
    data = json.loads(p.read_text(encoding="utf-8")); data["count"] = 99
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="summary_mismatch"):
        reprocess.reprocess(source, storage=target)
    assert not target.db_path.exists()


def test_replay_chroma_bm25_region_fallback_ack_and_unrelated(a_storage, target, monkeypatch, tmp_vectorstore, fake_embedder):
    source = acquire(a_storage, monkeypatch)
    seed_legacy(target, source.name)
    store.upsert_services([service("unrelated")], storage=target)
    reprocess.reprocess(source, storage=target, today=TODAY)
    token = next(e for e in store.get_pending_changes(storage=target) if e["service_id"] == "P")
    assert "region" not in token["chunks"][0]["metadata"]
    settings = handoff.Settings.resolve(db_path=target.db_path, index_dir=tmp_vectorstore)
    # Established worker-core test pattern: fixture config already selects the isolated index.
    settings = replace(settings, validation=True)
    result = handoff.apply_pending(settings, BIndexAdapter())
    assert not result["failed"] and result["acknowledged"] == 2
    assert index.get_service_chunks("P")[0].metadata["region"] == "서울"
    assert index.get_service_chunks("unrelated") and not store.get_pending_changes(storage=target)
    questions = [{"id": "synthetic", "relevant_service_ids": ["P", "absent"]}]
    checked = coverage.inspect_ids(target.db_path, tmp_vectorstore, questions, source_run=source)
    assert checked["rows"][0]["state"] == "present_in_index"
    assert checked["rows"][1]["state"] == "not_observed_in_available_sample"
    before = store.get_record("P", storage=target)
    reprocess.reprocess(source, storage=target, today=TODAY)
    assert not store.get_pending_changes(storage=target) and store.get_record("P", storage=target)["version"] == before["version"]


def test_replay_participates_in_source_lock(a_storage, target, monkeypatch):
    source = acquire(a_storage, monkeypatch)
    with store.ingestion_lock(storage=target):
        with pytest.raises(RuntimeError, match="already_running"):
            reprocess.reprocess(source, storage=target)


def test_replay_live_alias_rejected_before_writes(a_storage, monkeypatch, tmp_path):
    from src.common import config
    source = acquire(a_storage, monkeypatch)
    bad = StoragePaths(config.DB_PATH.parent / "alias" / ".." / config.DB_PATH.name,
                       tmp_path / "safe-replay", "validation")
    with pytest.raises(ValueError, match="live_storage"):
        reprocess.reprocess(source, storage=bad)
    assert not config.DB_PATH.exists() and not (tmp_path / "safe-replay").exists()


def test_compact_coverage_has_no_api_model_ack_or_index_side_effects(a_storage, target, monkeypatch, tmp_path):
    from src.index import embedder
    source = acquire(a_storage, monkeypatch)
    reprocess.reprocess(source, storage=target, today=TODAY)
    before = store.get_pending_changes(storage=target)

    def forbidden(*args, **kwargs):
        pytest.fail("Inspection invoked a mutating/network/model boundary")

    monkeypatch.setattr(client, "_get", forbidden)
    monkeypatch.setattr(embedder, "_get_model", forbidden)
    monkeypatch.setattr(index, "_collection", forbidden)
    monkeypatch.setattr(store, "acknowledge_changes", forbidden)
    absent_index = tmp_path / "not-created-index"
    result = coverage.inspect_ids(target.db_path, absent_index,
        [{"id": "diagnostic", "relevant_service_ids": ["P"]}], source_run=source)
    assert result["rows"][0]["state"] == "pending_application"
    assert store.get_pending_changes(storage=target) == before
    assert not absent_index.exists()


def test_intermediate_raw_failure_can_replay_accepted_rows_without_closure(a_storage, target, monkeypatch):
    source = acquire(a_storage, monkeypatch)
    # Original acquisition accepted page one but failed before page two.
    summary_path = source / "run-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    for name in client.ENDPOINTS:
        page_path = source / (name + "-00001.json")
        page = json.loads(page_path.read_text(encoding="utf-8"))
        page.update(perPage=1, totalCount=2)
        page_path.write_text(json.dumps(page), encoding="utf-8")
        endpoint = summary["endpoints"][name]
        endpoint.update(status="partial", total=2, failed_page=2, diagnostics=["http_503"])
        (source / (name + "-summary.json")).write_text(json.dumps(endpoint), encoding="utf-8")
    summary["status"] = "partial"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    result = reprocess.reprocess(source, storage=target, today=TODAY)
    assert result["new"] == 1 and result["source_run_status"] == "partial"
    assert not result["endpoints"]["serviceList"]["can_close_missing"]


def test_replay_cli_and_explicit_source_boundary(a_storage, target, monkeypatch):
    source = acquire(a_storage, monkeypatch)
    monkeypatch.setattr(client, "_get", lambda *a, **k: pytest.fail("Live API called"))
    assert reprocess.main(["--source-run", str(source), "--db-path", str(target.db_path),
        "--raw-dir", str(target.raw_dir), "--limit", "1", "--today", TODAY, "--validation"]) == 0
    bad = StoragePaths(target.db_path, source / "output", "test")
    with pytest.raises(ValueError, match="original_source"):
        reprocess.reprocess(source, storage=bad)

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.common import config
from src.ingest import client, pipeline, store

FIXTURE = Path("tests/fixtures/a_synthetic_api.json")


def service(sid="A", **overrides):
    return store.clean_service({"서비스ID": sid, "서비스명": "청년 지원", "지원대상": "청년",
        "지원내용": "내용", "소관기관명": "기관", "상세조회URL": "https://www.gov.kr/synthetic/" + sid,
        **overrides})


def listing(ids, **overrides):
    return client.Collection("serviceList", [{"서비스ID": sid} for sid in ids],
        status=overrides.get("status", "complete"), total=len(ids), pages=1,
        verified=overrides.get("verified", True), authoritative=True,
        limited=overrides.get("limited", False))


def apply(services, snapshot, *, full=False, today="2026-10-19"):
    run_id = client.new_run_id()
    store.start_run(run_id)
    return store.apply_run(run_id, services, snapshot, full=full, today=today,
                           summary={"run_id": run_id, "status": "complete"})


def test_idempotent_metadata_and_provenance_changes():
    value = service()
    assert store.upsert_services([value]) == ["A"]
    first = store.get_pending_changes()[0]
    assert store.upsert_services([value]) == []
    assert store.get_pending_changes()[0] == first
    assert store.upsert_services([service(소관기관명="새 기관")]) == ["A"]
    assert store.get_pending_changes()[0]["version"] == first["version"] + 1
    assert store.upsert_services([service(소관기관명="새 기관", 상세조회URL="https://www.gov.kr/synthetic/new")]) == ["A"]
    assert store.upsert_services([service(소관기관명="새 기관", 상세조회URL="https://www.gov.kr/synthetic/new", 신청기한="예산 소진 시")]) == ["A"]


def test_eligibility_only_change_detected():
    raw = {"서비스ID": "A", "서비스명": "청년 지원", "지원대상": "청년"}
    first = store.clean_service(raw)
    second = store.clean_service(raw, {"_verified_eligibility": {"source": "applicant", "region": "전국"}})
    assert store.upsert_services([first]) == ["A"]
    assert store.upsert_services([second]) == ["A"]
    assert store.get_pending_changes()[0]["chunks"][0]["metadata"]["region"] == "전국"


def test_fake_consumer_restart_and_version_aware_ack():
    store.upsert_services([service(지원대상="청년 " * 1200)])
    token = store.get_pending_changes()[0]
    assert len(token["chunks"]) > 1
    vector, bm25 = {}, {}

    def consume(event, fail=False):
        # Service replacement removes ALL old chunk IDs before applying this version.
        vector[event["service_id"]] = event["chunks"]
        if fail:
            raise RuntimeError("simulated BM25 failure")
        bm25[event["service_id"]] = event["chunks"]
        return store.acknowledge_changes([event])

    with pytest.raises(RuntimeError):
        consume(token, fail=True)
    assert store.get_pending_changes() == [token]
    child = subprocess.run([sys.executable, "-c",
        "import sys,json; from pathlib import Path; from src.common import config; "
        "config.DB_PATH=Path(sys.argv[1]); from src.ingest import store; "
        "print(json.dumps(store.get_pending_changes(),ensure_ascii=True))", str(config.DB_PATH)],
        capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == [token]  # separate interpreter/process restart
    assert store.upsert_services([service(지원대상="청년", 지원내용="")]) == ["A"]
    newer = store.get_pending_changes()[0]
    assert len(newer["chunks"]) == 1
    assert store.acknowledge_changes([token]) == 0
    assert store.get_pending_changes() == [newer]
    assert consume(newer) == 1
    assert vector == bm25 and store.get_pending_changes() == []
    assert store.pending_chunks() == []


def test_long_section_becomes_empty_still_replaces_whole_service():
    store.upsert_services([service(지원대상="청년 " * 1000)])
    store.upsert_services([service(지원대상="", 지원내용="", 서비스명="청년 지원")])
    event = store.get_pending_changes()[0]
    assert event["action"] == "replace" and event["chunks"] == []


@pytest.mark.parametrize("overrides", [{"status": "sample"}, {"status": "partial"},
    {"status": "failed"}, {"verified": False}, {"limited": True}])
def test_unsafe_snapshots_never_close_missing(overrides):
    store.upsert_services([service("A"), service("B")])
    apply([], listing(["B"], **overrides))
    assert {s["service_id"] for s in store.load_services()} == {"A", "B"}
    assert store.mark_closed() == []


def test_distinct_reasons_and_reopening():
    store.upsert_services([service("missing"), service("expired"), service("scope"), service("outside")])
    old = store.get_pending_changes()
    store.acknowledge_changes(old)
    # Compare disappearance against ALL listing IDs, including non-youth records.
    summary = apply([service("expired", 신청기한="2020-01-01"),
                     service("scope", 서비스명="기관 운영 지원", 지원대상="법인만"),
                     service("outside", 서비스명="기관 운영 지원", 지원대상="법인만")],
                    listing(["expired", "scope", "outside"]))
    assert summary["removals"] == {"expired": 1, "policy_scope_exit": 2, "api_disappeared": 1}
    reasons = {e["service_id"]: e["reason"] for e in store.get_pending_changes()}
    assert reasons == {"missing": "api_disappeared", "expired": "expired", "scope": "policy_scope_exit", "outside": "policy_scope_exit"}
    assert store.load_services() == []
    assert "support_target" not in store.get_record("scope")["payload"]
    assert store.get_record("expired")["payload"]["application_period"] == "2020-01-01"
    assert set(store.pending_removal_ids()) == set(reasons)
    assert store.upsert_services([service("missing"), service("expired"), service("scope")]) == ["missing", "expired", "scope"]
    assert all(e["action"] == "replace" for e in store.get_pending_changes() if e["service_id"] != "outside")


def test_explicit_institution_only_minimal_audit():
    assert store.upsert_services([service(서비스명="기관 운영 지원", 지원대상="공공기관만")]) == []
    assert store.load_services() == [] and store.get_pending_changes() == []
    assert store.get_record("A")["reason"] == "policy_scope_exit"
    assert "support_target" not in store.get_record("A")["payload"]


def test_missing_auxiliary_does_not_exclude_or_reopen():
    store.upsert_services([service()])
    uncertain = service(서비스명="기관 운영 지원", 지원대상="법인만")
    uncertain["_inclusion_complete"] = False
    assert apply([uncertain], listing(["A"]))["deferred"] == 1
    assert len(store.load_services()) == 1
    apply([], listing(["B"]))
    uncertain = service()
    uncertain["_inclusion_complete"] = False
    apply([uncertain], listing(["A"]))
    assert store.load_services() == []


def test_transaction_rollback_and_interrupted_recovery():
    run_id = client.new_run_id()
    store.start_run(run_id)
    with pytest.raises(ValueError, match="duplicate"):
        store.apply_run(run_id, [service(), service()], listing(["A"]), full=False,
                        today="2026-10-19", summary={"status": "complete"})
    assert store.load_services() == [] and store.get_pending_changes() == []
    assert store.get_run(run_id)["status"] == "running"
    store.start_run(client.new_run_id())
    assert store.get_run(run_id)["status"] == "interrupted"


def test_overlapping_ingestion_blocked():
    with store.ingestion_lock():
        with pytest.raises(RuntimeError, match="already_running"):
            with store.ingestion_lock():
                pytest.fail("lock allowed overlap")


def test_load_keyword_literal_matching_and_order():
    store.upsert_services([service("B", 지원내용="100% 확정 아님"), service("A", 지원내용="100만 원")])
    assert store.load_services([]) == []
    assert [s["service_id"] for s in store.load_services(["B"])] == ["B"]
    assert [s["service_id"] for s in store.sql_keyword_search(["100%"], 5)] == ["B"]
    assert [s["service_id"] for s in store.sql_keyword_search(["청년", "100%"], 1)] == ["A"]
    assert store.sql_keyword_search(["' OR 1=1 --"], 5) == []
    assert store.sql_keyword_search([], 5) == []
    assert store.sql_keyword_search(["청년"], 0) == []


def test_full_preserves_database_history_and_old_ack_cannot_discard_full():
    store.upsert_services([service(), service("B")])
    apply([], listing(["A"]))
    removal = next(e for e in store.get_pending_changes() if e["service_id"] == "B")
    first = store.get_pending_changes()[0]
    apply([], listing(["A"], verified=False), full=True)
    newer = store.get_pending_changes()[0]
    assert first["version"] == newer["version"] and first["event_id"] != newer["event_id"]
    assert store.acknowledge_changes([first]) == 0
    assert len(store.load_services()) == 1
    assert next(e for e in store.get_pending_changes() if e["service_id"] == "B") == removal


def test_endpoint_id_join_failed_details_retained_and_fresh_empty_clears(monkeypatch, a_storage):
    fetch = pipeline._fixture_fetch(FIXTURE)
    first_chunks, first_summary = pipeline._execute(fetch_page=fetch, storage=a_storage)
    assert first_chunks and first_summary["new"] == 2
    values = {s["service_id"]: s for s in store.load_services(storage=a_storage)}
    assert "10만 원" in values["A-SYN-001"]["support_content"]
    assert values["A-SYN-002"]["support_content"] == "상담 지원"
    assert "age_min" not in first_chunks[0].metadata  # JA meaning unverified
    assert first_summary["diagnostics"]["broad_keyword_examples"]
    tokens = store.get_pending_changes(storage=a_storage)

    def failed_detail(endpoint, **params):
        if endpoint == "serviceDetail":
            raise client.CollectionError("http_503")
        return fetch(endpoint, **params)

    replay, summary = pipeline._execute(fetch_page=failed_detail, storage=a_storage)
    assert summary["status"] == "partial" and summary["changed"] == 0
    assert replay == first_chunks and store.get_pending_changes(storage=a_storage) == tokens
    assert store.load_services(["A-SYN-001"], storage=a_storage)[0]["_observations"]["serviceDetail"] == "retained"
    assert store.load_services(["A-SYN-001"], storage=a_storage)[0]["support_content"] == values["A-SYN-001"]["support_content"]

    def empty_detail(endpoint, **params):
        data = fetch(endpoint, **params)
        if endpoint == "serviceDetail":
            data = {**data, "data": [{"서비스ID": "A-SYN-002"}, {"서비스ID": "A-SYN-001"}]}
        return data

    pipeline._execute(fetch_page=empty_detail, storage=a_storage)
    assert store.load_services(["A-SYN-001"], storage=a_storage)[0]["support_content"] == ""
    assert all(not c["text"].endswith("상담 지원") for e in store.get_pending_changes(storage=a_storage) for c in e["chunks"])


def test_pipeline_public_run_and_cli_default_full(monkeypatch, capsys):
    fetch = pipeline._fixture_fetch(FIXTURE)
    monkeypatch.setattr(client, "_get", fetch)
    chunks = pipeline.run()
    assert isinstance(chunks, list) and chunks
    assert pipeline.run() == chunks  # zero changed, pending replay
    assert pipeline.main(["--offline-fixture", str(FIXTURE), "--full"]) == 0
    assert "동일 2" in capsys.readouterr().out
    assert pipeline.main(["--offline-fixture", str(FIXTURE), "--sample-pages", "1"]) == 0
    with pytest.raises(SystemExit) as exc:
        pipeline.main(["--sample-pages", "0"])
    assert exc.value.code == 2


@pytest.mark.parametrize("extra", [[], ["--full"]])
def test_module_cli_subprocess_no_credentials(extra, tmp_path):
    db_path, raw_dir = tmp_path / "offline.db", tmp_path / "offline-raw"
    env = {**os.environ, "GOV24_API_KEY": "", "OPENAI_API_KEY": "", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "-m", "src.ingest.pipeline", "--db-path", str(db_path), "--raw-dir", str(raw_dir),
                             "--offline-fixture", str(FIXTURE), *extra],
                            capture_output=True, text=True, encoding="utf-8", env=env, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "상태: complete" in result.stdout
    assert db_path.exists() and not config.DB_PATH.exists()


def test_no_a_stubs_remain():
    for path in Path("src/ingest").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(isinstance(node, ast.Name) and node.id == "NotImplementedError" for node in ast.walk(tree)), path

"""Real Chroma/B index integration; only embeddings and failure boundaries injected."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from src.common import config
from src.common.schema import Chunk
from src.ingest import handoff, store
from src.ingest.handoff_index import BIndexAdapter
from src.index import vector_store as index
from tests.ingest.test_store_pipeline import service, listing


@pytest.fixture
def setup_handoff(a_storage, tmp_vectorstore, fake_embedder):
    settings = handoff.Settings.resolve(db_path=a_storage.db_path, index_dir=tmp_vectorstore,
                                        validation=False, adopt_existing=True)
    # tests' tmp_vectorstore becomes the configured default; all paths are pytest
    # temporary paths, not the actual repository defaults. Production --validation
    # checks are tested separately against protected sentinel paths.
    return settings, BIndexAdapter(), a_storage


def put(paths, sid="A", **values):
    store.upsert_services([service(sid, **values)], storage=paths)
    return store.get_pending_changes(storage=paths)[-1]


def apply(setup):
    settings, adapter, _ = setup
    report = handoff.apply_pending(settings, adapter)
    assert report["failed"] == [], report
    return report


def ids(sid):
    return {c.chunk_id for c in index.get_service_chunks(sid)}


def enqueue(paths, *, sid="A", action="replace", chunks=None):
    with store.connection(storage=paths) as con:
        store._enqueue(con, sid, 2, action, "test", chunks or [])
    return store.get_pending_changes(storage=paths)[0]


def test_initial_exact_ack_idempotence_and_c_expansion(setup_handoff):
    settings, adapter, paths = setup_handoff
    event = put(paths, 지원대상="청년 월세 지원", 선정기준="소득 심사", 지원내용="매월 이십만원", 신청방법="온라인 접수")
    report = apply(setup_handoff)
    assert report["acknowledged"] == report["applied"] == 1
    assert report["events"][0]["event_id"] == event["event_id"]
    assert handoff.read_pending(settings) == []
    assert ids("A") == {c["chunk_id"] for c in event["chunks"]}
    from src.generate import evidence
    hits = [index.vector_search("월세", 1)[0][0]]
    expanded = evidence.select_evidence(hits)
    assert {c.section for c in expanded["A"]} == {"지원대상", "선정기준", "지원내용", "신청방법"}
    marker = index._read_version()
    assert apply(setup_handoff)["applied"] == 0
    assert index._read_version() == marker


def test_shorter_sections_and_metadata_replace_entire_service(setup_handoff):
    _, _, paths = setup_handoff
    put(paths, 지원대상="청년 " * 1500)
    apply(setup_handoff)
    old = ids("A")
    event = put(paths, 지원대상="청년 짧은 본문", 소관기관명="새 기관")
    apply(setup_handoff)
    assert len(ids("A")) < len(old)
    assert ids("A") == {c["chunk_id"] for c in event["chunks"]}
    assert all(c.metadata["agency"] == "새 기관" for c in index.get_service_chunks("A"))


@pytest.mark.parametrize("action", ["replace", "remove"])
def test_empty_replace_or_remove_preserves_unrelated_and_cache(setup_handoff, action):
    _, _, paths = setup_handoff
    put(paths, "A", 지원내용="월세 지원")
    put(paths, "B", 지원내용="교육 훈련")
    apply(setup_handoff)
    old_bm25 = index._bm25_index()[0]
    unrelated = [c.to_dict() for c in index.get_service_chunks("B")]
    enqueue(paths, action=action)
    report = apply(setup_handoff)
    assert report["acknowledged"] == 1
    assert not ids("A")
    assert index._bm25_index()[0] is not old_bm25
    assert not any(cid.startswith("A#") for cid in index._bm25_index()[1])
    assert index.vector_search("월세", 10, where={"service_id": "A"}) == []
    assert index.bm25_search("월세", 10, where={"service_id": "A"}) == []
    assert [c.to_dict() for c in index.get_service_chunks("B")] == unrelated
    assert {c.chunk_id for c, _ in index.vector_search("교육", 10, where={"service_id": "B"})} == ids("B")


def test_positive_bm25_visibility_and_cache_update(setup_handoff):
    _, _, paths = setup_handoff
    for sid, text in [("A", "월세주택보증금"), ("B", "교육직업훈련"), ("C", "건강의료검진"), ("D", "농업종자재배")]:
        put(paths, sid, 지원내용=text)
    apply(setup_handoff)
    assert any(c.service_id == "A" for c, _ in index.bm25_search("월세주택", 20))
    old = index._bm25_index()[0]
    put(paths, 지원내용="금융대출이자감면")
    apply(setup_handoff)
    assert index._bm25_index()[0] is not old
    assert any(c.service_id == "A" for c, _ in index.bm25_search("대출이자", 20))
    assert not any(c.service_id == "A" for c, _ in index.bm25_search("월세주택", 20))


@pytest.mark.parametrize("stage", ["apply", "verify", "acknowledge"])
def test_failure_retains_exact_pending_and_replay(setup_handoff, monkeypatch, stage):
    settings, adapter, paths = setup_handoff
    event = put(paths)
    with monkeypatch.context() as patch:
        if stage == "acknowledge":
            patch.setattr(store, "acknowledge_changes", lambda *a, **kw: 0)
        else:
            patch.setattr(adapter, stage, Mock(side_effect=RuntimeError("injected_failure")))
        report = handoff.apply_pending(settings, adapter)
    assert report["failed"][0]["stage"] == stage
    assert report["acknowledged"] == 0
    assert handoff.read_pending(settings) == [event]
    # A fresh adapter represents a restart, after possibly successful index apply.
    assert handoff.apply_pending(settings, BIndexAdapter())["acknowledged"] == 1


def test_partial_index_application_retry_converges(setup_handoff, monkeypatch):
    settings, adapter, paths = setup_handoff
    event = put(paths, 선정기준="소득 심사", 신청방법="온라인")
    original = index.build_index

    def partial(chunks):
        original(chunks[:1])
        raise RuntimeError("after_first_batch")

    with monkeypatch.context() as patch:
        patch.setattr(index, "build_index", partial)
        assert handoff.apply_pending(settings, adapter)["failed"][0]["stage"] == "apply"
    assert 0 < len(ids("A")) < len(event["chunks"])
    assert handoff.read_pending(settings) == [event]
    apply(setup_handoff)
    assert ids("A") == {c["chunk_id"] for c in event["chunks"]}


@pytest.mark.parametrize("corrupt", ["text", "metadata", "stale", "bm25", "vector", "keyword"])
def test_verification_detects_corruption(setup_handoff, monkeypatch, corrupt):
    settings, adapter, paths = setup_handoff
    event = put(paths)
    original = adapter.apply

    def broken(event, chunks):
        original(event, chunks)
        if corrupt == "text":
            embedding = index._collection().get(ids=[chunks[0].chunk_id], include=["embeddings"])["embeddings"]
            index._collection().update(ids=[chunks[0].chunk_id], documents=["wrong text"], embeddings=embedding)
        elif corrupt == "metadata":
            index._collection().update(ids=[chunks[0].chunk_id], metadatas=[{"agency": "wrong"}])
        elif corrupt == "stale":
            index.build_index(chunks + [Chunk("A#지원내용-999", "A", "청년 지원", "지원내용", "청년 오래된 조각")])
        elif corrupt == "bm25":
            # Pin a stale corpus under the NEW marker to bypass normal rebuilding.
            key = str(index._store_dir()) + "|" + index._collection_name()
            index._bm25_cache[key] = (index._read_version(), None, [])
        elif corrupt == "vector":
            monkeypatch.setattr(index, "vector_search", lambda *a, **kw: [])
        else:
            monkeypatch.setattr(index, "bm25_search", lambda *a, **kw: [(chunks[0], 1.0)])

    monkeypatch.setattr(adapter, "apply", broken)
    report = handoff.apply_pending(settings, adapter)
    assert report["failed"][0]["stage"] == "verify"
    assert report["acknowledged"] == 0
    assert handoff.read_pending(settings) == [event]


def test_supersession_revalidates_token_and_stale_ack_fails(setup_handoff, monkeypatch):
    settings, adapter, paths = setup_handoff
    old = put(paths)
    original = adapter.prepare

    def new_event(*args, **kwargs):
        # Simulate an unsupported direct writer; supported ingestion is locked out.
        enqueue(paths, chunks=[Chunk("A#지원대상", "A", "청년", "지원대상", "청년 최신")])
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "prepare", new_event)
    report = handoff.apply_pending(settings, adapter)
    assert report["failed"][0]["stage"] == "revalidate"
    assert report["applied"] == 0
    assert store.acknowledge_changes([old], storage=paths) == 0
    assert len(handoff.read_pending(settings)) == 1


@pytest.mark.parametrize("bad", ["missing", "null", "wrong_service", "wrong_chunk_id", "duplicate", "unknown_metadata", "remove_payload"])
def test_invalid_events_fail_before_initializing_index(setup_handoff, bad):
    settings, adapter, paths = setup_handoff
    event = put(paths)
    if bad == "missing":
        del event["chunks"]
    elif bad == "null":
        event["chunks"] = None
    elif bad == "wrong_service":
        event["chunks"][0]["service_id"] = "OTHER"
    elif bad == "wrong_chunk_id":
        event["chunks"][0]["chunk_id"] = "A#지원대상#지원내용"
    elif bad == "duplicate":
        event["chunks"].append(event["chunks"][0])
    elif bad == "unknown_metadata":
        event["chunks"][0]["metadata"]["unpersisted"] = "must not be lost"
    else:
        event["action"] = "remove"
    with pytest.raises(ValueError):
        handoff.validate_event(event)
    assert not Path(settings.index_dir).exists()


def test_dry_run_no_index_model_or_ack_and_cli(setup_handoff, monkeypatch, capsys):
    settings, _, paths = setup_handoff
    event = put(paths)
    before = paths.db_path.read_bytes()
    monkeypatch.setattr(index, "_collection", Mock(side_effect=AssertionError("index initialized")))
    monkeypatch.setattr(handoff.subprocess, "run", Mock(side_effect=AssertionError("worker started")))
    assert handoff.consume(settings)["events"][0]["chunks"] == len(event["chunks"])
    assert handoff.main(["--db-path", settings.db_path, "--index-dir", settings.index_dir]) == 0
    assert '"mode": "dry-run"' in capsys.readouterr().out
    assert paths.db_path.read_bytes() == before
    assert not Path(settings.index_dir).exists()
    assert not Path(settings.db_path + ".handoff.json").exists()
    assert handoff.read_pending(settings) == [event]


def test_source_index_model_binding_no_multi_target_or_model_switch(setup_handoff):
    settings, adapter, paths = setup_handoff
    put(paths)
    apply(setup_handoff)
    for conflicting in [replace(settings, model="other/model"),
                        replace(settings, index_dir=str(Path(settings.index_dir).with_name("other-index")))]:
        assert handoff.apply_pending(conflicting, adapter)["failed"][0]["stage"] == "association"
    other = replace(settings, db_path=str(paths.db_path.with_name("other.db")))
    put(other.storage())
    assert handoff.apply_pending(other, adapter)["failed"][0]["stage"] == "association"


def test_existing_index_requires_adoption_and_dimension_mismatch(setup_handoff, monkeypatch):
    settings, adapter, paths = setup_handoff
    index.build_index([Chunk("B#지원내용", "B", "교육", "지원내용", "교육 훈련")])
    put(paths)
    rejected = handoff.apply_pending(replace(settings, adopt_existing=False), adapter)
    assert rejected["failed"][0]["code"] == "existing_index_requires_explicit_adoption"
    from src.index import embedder
    monkeypatch.setattr(embedder, "embedding_dim", lambda: 2)
    assert handoff.apply_pending(settings, adapter)["failed"][0]["code"] == "incompatible_embedding_dimension"
    assert ids("B") and handoff.read_pending(settings)


def test_validation_rejects_live_aliases_without_writes(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    db, vector, raw = live / "services.db", live / "vector", live / "raw"
    db.write_bytes(b"live sentinel")
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "VECTORSTORE_DIR", vector)
    monkeypatch.setattr(config, "RAW_DIR", raw)
    for selected_db, selected_index in [(db, tmp_path / "safe"),
            (db.parent / "alias" / ".." / db.name, tmp_path / "safe"),
            (tmp_path / "safe.db", vector), (tmp_path / "safe.db", raw / "nested")]:
        with pytest.raises(ValueError):
            handoff.Settings.resolve(db_path=selected_db, index_dir=selected_index, validation=True)
    assert db.read_bytes() == b"live sentinel"
    assert not vector.exists() and not raw.exists() and not (tmp_path / "safe").exists()
    assert handoff.Settings.resolve().db_path == str(db.resolve())
    assert handoff.Settings.resolve().index_dir == str(vector.resolve())


def child(mode, settings_or_path, *, pipe=True):
    argument = json.dumps(asdict(settings_or_path)) if isinstance(settings_or_path, handoff.Settings) else str(settings_or_path)
    return subprocess.Popen([sys.executable, "-m", "tests.ingest.handoff_child", mode, argument],
                            stdin=subprocess.PIPE if pipe else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")


@pytest.mark.parametrize("which", ["index", "source"])
def test_cross_process_locks_fail_bounded_and_release_on_termination(setup_handoff, which):
    settings, adapter, paths = setup_handoff
    put(paths)
    lock = settings.index_dir + ".handoff.lock" if which == "index" else settings.db_path + ".ingest.lock"
    holder = child("lock", lock)
    try:
        assert holder.stdout.readline().strip() == "locked"
        report = handoff.apply_pending(settings, adapter)
        assert report["failed"][0]["stage"] == "lock"
        assert report["applied"] == report["acknowledged"] == 0
    finally:
        holder.kill()
        holder.communicate(timeout=15)
    assert apply(setup_handoff)["acknowledged"] == 1


def test_worker_holds_source_lock_ingestion_cannot_write_stale_snapshot(setup_handoff):
    settings, _, paths = setup_handoff
    old = put(paths)
    process = child("pause", settings)
    try:
        assert process.stdout.readline().strip() == "protected"
        competing = handoff.apply_pending(settings, BIndexAdapter())
        assert competing["failed"][0]["code"] == "index_consumer_already_running"
        for mutate in [lambda: put(paths, 지원내용="newer"),
                       lambda: store.apply_run("test", [service(지원내용="newer")], listing(["A"]),
                                               full=False, today="2026-10-20", summary={"status": "complete"}, storage=paths)]:
            with pytest.raises(RuntimeError, match="ingestion_already_running"):
                mutate()
        process.stdin.write("continue\n")
        process.stdin.flush()
        output, error = process.communicate(timeout=45)
        assert process.returncode == 0, error
        assert json.loads(output)["acknowledged"] == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=15)
    newer = put(paths, 지원내용="청년 최신 지원금")
    assert newer["version"] > old["version"]
    assert store.acknowledge_changes([old], storage=paths) == 0
    assert apply(setup_handoff)["acknowledged"] == 1
    assert any("최신" in c.text for c in index.get_service_chunks("A"))


def test_process_crash_after_apply_before_ack_replays(setup_handoff):
    settings, _, paths = setup_handoff
    old = put(paths)
    process = child("crash", settings, pipe=False)
    _, error = process.communicate(timeout=45)
    assert process.returncode == 9, error
    assert handoff.read_pending(settings) == [old]
    assert apply(setup_handoff)["acknowledged"] == 1


def test_application_cli_nonzero_and_no_caller_global_mutation(setup_handoff, monkeypatch, capsys):
    settings, _, paths = setup_handoff
    put(paths)
    original = config.VECTORSTORE_DIR
    report = {"applied": 0, "acknowledged": 0, "failed": [{"stage": "apply"}], "remaining": 1}
    runner = Mock(return_value=subprocess.CompletedProcess([], 1, json.dumps(report), ""))
    monkeypatch.setattr(handoff.subprocess, "run", runner)
    assert handoff.main(["--db-path", settings.db_path, "--index-dir", settings.index_dir, "--apply"]) == 1
    assert config.VECTORSTORE_DIR == original
    assert runner.call_args.kwargs["env"]["HF_HUB_OFFLINE"] == "1"
    assert handoff.read_pending(settings)


def test_real_worker_protocol_isolated_live_sentinels_and_cross_process_cache(setup_handoff, monkeypatch, tmp_path):
    settings, _, paths = setup_handoff
    event = put(paths, 지원내용="청년 주택 임대료")
    run = subprocess.run

    def fake_embedding_worker(command, **kwargs):
        assert command[-1] == "src.ingest.handoff_worker"
        return run([sys.executable, "-m", "tests.ingest.handoff_child", "worker", "{}"], **kwargs, timeout=45)

    # All storage under these protected defaults must remain byte-for-byte intact.
    live = tmp_path / "protected-live"
    live.mkdir()
    for name in ("live.db", "raw/page.json", "vector/chroma.sqlite3", "live.db-wal", "vector/index_version"):
        sentinel = live / name
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_bytes(b"untouched-live-sentinel")
    before = {str(p.relative_to(live)): p.read_bytes() for p in live.rglob("*") if p.is_file()}
    with monkeypatch.context() as patch:
        patch.setattr(config, "DB_PATH", live / "live.db")
        patch.setattr(config, "RAW_DIR", live / "raw")
        patch.setattr(config, "VECTORSTORE_DIR", live / "vector")
        patch.setattr(handoff.subprocess, "run", fake_embedding_worker)
        isolated = replace(settings, validation=True)
        assert handoff.consume(isolated, apply=True)["acknowledged"] == 1
        assert config.VECTORSTORE_DIR == live / "vector"
    assert {str(p.relative_to(live)): p.read_bytes() for p in live.rglob("*") if p.is_file()} == before
    assert ids("A") == {c["chunk_id"] for c in event["chunks"]}
    previous = index._bm25_index()[0]
    put(paths, 지원내용="청년 최신 취업 수당")
    with monkeypatch.context() as patch:
        patch.setattr(handoff.subprocess, "run", fake_embedding_worker)
        assert handoff.consume(settings, apply=True)["acknowledged"] == 1
    assert index._bm25_index()[0] is not previous
    assert any("취업" in c.text for c in index.get_service_chunks("A"))


def test_synthetic_source_requires_validation_before_locks_or_index(setup_handoff):
    settings, adapter, paths = setup_handoff
    put(paths)
    store.start_run("synthetic", storage=paths)
    store.finish_run("synthetic", "sample", {"mode": "offline"}, storage=paths)
    store.start_run("later-live", storage=paths)
    store.finish_run("later-live", "failed", {"mode": "live"}, storage=paths)
    report = handoff.apply_pending(settings, adapter)
    assert report["failed"][0]["code"] == "isolated_source_requires_validation_mode"
    assert not Path(settings.index_dir).exists()
    assert not Path(settings.index_dir + ".handoff.lock").exists()


def test_hardlinked_source_cannot_bypass_queue_association(setup_handoff):
    settings, _, paths = setup_handoff
    put(paths)
    import os
    alias = paths.db_path.with_name("alias.db")
    os.link(paths.db_path, alias)
    with pytest.raises(ValueError, match="source_db_hardlinks_not_supported"):
        handoff.Settings.resolve(db_path=alias, index_dir=settings.index_dir)


def test_index_lock_cannot_alias_source_database(setup_handoff):
    settings, _, paths = setup_handoff
    collision = paths.db_path.with_name("index.handoff.lock")
    with pytest.raises(ValueError, match="source_index_runtime_paths_overlap"):
        handoff.Settings.resolve(db_path=collision, index_dir=paths.db_path.with_name("index"))


def test_other_model_collection_is_rejected_without_creating_new_collection(setup_handoff, monkeypatch):
    settings, adapter, paths = setup_handoff
    index.build_index([Chunk("B#지원내용", "B", "교육", "지원내용", "교육 훈련")])
    put(paths)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "different-model")
    changed = replace(settings, model="different-model")
    report = handoff.apply_pending(changed, adapter)
    assert report["failed"][0]["code"] == "existing_index_model_not_found"
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    client = chromadb.PersistentClient(path=settings.index_dir, settings=ChromaSettings(anonymized_telemetry=False))
    assert len(client.list_collections()) == 1


def test_stop_on_first_failure_leaves_later_events_pending(setup_handoff, monkeypatch):
    settings, adapter, paths = setup_handoff
    put(paths, "A")
    put(paths, "B")
    monkeypatch.setattr(adapter, "verify", Mock(side_effect=RuntimeError("stop_here")))
    report = handoff.apply_pending(settings, adapter)
    assert report["applied"] == 1 and report["acknowledged"] == 0 and report["remaining"] == 2
    assert ids("B") == set()


def test_corrupt_stored_payload_fails_before_prepare(setup_handoff, monkeypatch):
    settings, adapter, paths = setup_handoff
    put(paths)
    with store.connection(storage=paths) as con:
        con.execute("UPDATE pending_changes SET chunks='null'")
    prepare = Mock(side_effect=AssertionError("must not initialize"))
    monkeypatch.setattr(adapter, "prepare", prepare)
    assert handoff.apply_pending(settings, adapter)["failed"][0]["stage"] == "validate"
    prepare.assert_not_called()
    assert not Path(settings.index_dir).exists()

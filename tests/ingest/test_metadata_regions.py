"""Region/deadline provenance, read-only diagnosis, and verified correction with real Chroma."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from src.common import config
from src.common.regions import normalize_region
from src.common.schema import Chunk
from src.ingest import handoff, metadata_tools, normalize, pipeline, store
from src.ingest.chunker import chunk_service
from src.ingest.handoff_index import BIndexAdapter
from src.retrieve import filters
from src.index import vector_store as index
from tests.ingest.test_store_pipeline import service, listing

TODAY = "2026-09-09"


@pytest.mark.parametrize("value,expected", [
    ("서울특별시", "서울"), ("서울 특별시", "서울"), ("서울", "서울"), ("서울시", "서울"), ("부산광역시", "부산"),
    ("강원도", "강원"), ("강원특별자치도", "강원"), ("전라북도", "전북"), ("전북특별자치도", "전북"),
    ("제주특별자치도", "제주"), ("제주시", "제주"), ("충북도", "충북"), ("경기도 용인시", "경기"), ("서울특별시 강남구", "서울"),
    ("전국", "전국"), ("미상", None), (None, None), ("서울 또는 부산", None), ("한국", None), ("강남구", None)])
def test_canonical_aliases_unknown_and_idempotence(value, expected):
    assert normalize_region(value) == expected
    assert normalize_region(normalize_region(value)) == expected


@pytest.mark.parametrize("target,expected", [
    ("서울특별시에 거주하는 청년", "서울"), ("신청자는 부산광역시에 거주하는 성인", "부산"),
    ("경기도 용인시 거주자", "경기"), ("전국 거주자", "전국"),
    ("서울특별시에 거주하는 자녀를 둔 부모", None), ("서울특별시 소재 학교 재학생", None),
    ("이전에 서울특별시에 거주하는 사람", None), ("신청 장소: 부산광역시청", None),
    ("서울특별시 거주자 제외", None), ("서울특별시 거주자 또는 부산광역시 거주자", None),
    ("서울특별시 거주자\n부산광역시 거주자", None), ("서울특별시에 거주하는 직원이 있는 기관", None),
    ("광주시 거주자", None), ("거주지 무관", None), ("서울특별시 거주자 및 타지역도 가능", None),
    ("서울특별시에 거주하는 부모를 둔 대학생", None), ("서울특별시에 거주하는 배우자가 있는 사람", None),
    ("서울특별시 거주자 (예외는 별도 공고)", None)])
def test_residence_scoping_and_misleading_counterexamples(target, expected):
    value = service(지원대상=target, 소관기관명="부산광역시")
    assert store.build_metadata(value).get("region") == expected
    assert value["support_target"] == target
    if expected:
        assert store.build_metadata(value)["region_provenance"] == "explicit_eligibility"
        assert value["eligibility"]["residence_evidence"][0]["source_phrase"] == target


def test_district_multiple_locations_and_endpoint_audit(a_storage):
    raw = {"서비스ID": "A", "서비스명": "가구 지원", "지원대상": "경기도 용인시 거주자", "소관기관명": "부산광역시"}
    from src.ingest.client import Collection
    collections = {name: Collection(name, [raw] if name != "supportConditions" else [{"서비스ID": "A"}])
                   for name in ("serviceList", "serviceDetail", "supportConditions")}
    value = pipeline._join(collections, storage=a_storage)[0][0]
    evidence = value["eligibility"]["residence_evidence"][0]
    assert evidence["endpoint"] == "serviceDetail" and evidence["field"] == "support_target"
    assert evidence["source_field"] == "지원대상"
    assert evidence["raw_region"] == "경기도 용인시"
    assert evidence["scope"] == "district_limited_province_approximation"
    assert store.build_metadata(value)["region"] == "경기"
    value["_field_provenance"]["support_target"]["observation"] = "historical_value"
    value["_field_provenance"]["support_target"]["source_acquisition"] = "another-audit-run"
    assert normalize.content_hash(normalize.refresh_service(value)) == value["content_hash"]
    multi = service(지원대상="서울특별시 강남구 거주자\n서울특별시 서초구 거주자")
    assert len(multi["eligibility"]["residence_evidence"]) == 2
    assert store.build_metadata(multi)["region"] == "서울"  # province approximation, both districts retained


@pytest.mark.parametrize("text,expected", [
    ("2026-09-09", "2026-09-09"), ("2026.9.9.", "2026-09-09"),
    ("2026년 9월 9일", "2026-09-09"), ("2026년9월9일", "2026-09-09"),
    ("2026년 9월 1일 ~ 2026년 9월 9일", "2026-09-09"),
    ("2026-09-01～2026년9월9일", "2026-09-09"),
    ("2026년9월10일 ~ 2026년9월9일", None), ("2026년2월29일", None),
    ("9월 9일", None), ("상시", None), ("예산 소진 시", None), ("별도 공고", None),
    ("2026년9월9일 또는 예산 소진 시", None), ("1차 2026년9월9일, 2차 2026년10월1일", None)])
def test_strict_deadline_grammar(text, expected):
    value = service(신청기한=text)
    assert value["application_period"] == text and value["deadline"] == expected


def test_deadline_hash_version_expiry_and_b_filter_inclusive(a_storage, monkeypatch):
    monkeypatch.setattr(pipeline, "seoul_today", lambda: "2026-09-08")
    value = service(신청기한="2026년9월9일")
    store.upsert_services([value], storage=a_storage)
    event = store.get_pending_changes(storage=a_storage)[0]
    chunk = Chunk(**event["chunks"][0])
    assert filters.apply_filters([chunk], {"today": "2026-09-08"})
    assert filters.apply_filters([chunk], {"today": TODAY})
    assert not filters.apply_filters([chunk], {"today": "2026-09-10"})
    assert index._to_store_metadata(chunk)["deadline_int"] == 20260909
    assert store.upsert_services([value], storage=a_storage) == []
    old_hash = store.get_record("A", storage=a_storage)["content_hash"]
    changed = service(신청기한="2026년9월10일")
    assert store.upsert_services([changed], storage=a_storage) == ["A"]
    newer = store.get_pending_changes(storage=a_storage)[0]
    assert newer["version"] == event["version"] + 1 and changed["content_hash"] != old_hash
    run_id = "expiry-test"
    store.start_run(run_id, storage=a_storage)
    store.apply_run(run_id, [], listing([], status="sample", limited=True), full=False, today="2026-09-11",
                    summary={"status": "sample"}, storage=a_storage)
    removal = store.get_pending_changes(storage=a_storage)[0]
    assert removal["action"] == "remove" and removal["reason"] == "expired"
    assert store.get_record("A", storage=a_storage)["reason"] != "api_disappeared"


def test_provenance_roundtrip_filters_and_unknown(fake_embedder, tmp_vectorstore):
    explicit = service("explicit", 지원대상="서울특별시 거주자", 소관기관명="부산광역시")
    inferred = service("inferred", 지원대상="개인", 소관기관명="경기도 용인시")
    unknown = service("unknown", 지원대상="개인", 소관기관명="중앙부처")
    chunks = [*chunk_service(explicit), *chunk_service(inferred), *chunk_service(unknown)]
    index.build_index(chunks)
    assert index.get_service_chunks("explicit")[0].metadata["region_provenance"] == "explicit_eligibility"
    assert index.get_service_chunks("explicit")[0].metadata["region"] == "서울"
    assert index.get_service_chunks("inferred")[0].metadata["region_provenance"] == "agency_inference"
    assert index.get_service_chunks("inferred")[0].metadata["region"] == "경기"
    assert "region" not in index.get_service_chunks("unknown")[0].metadata
    assert index.get_service_chunks("unknown")[0].metadata["region_provenance"] == "unknown"
    for user in ("서울", "서울특별시", "경기도", "미상"):
        criteria = {"region": user, "today": "2026-10-22"}
        python_ids = {c.chunk_id for c in filters.apply_filters(chunks, criteria)}
        stored_ids = set(index._collection().get(where=index.build_where(criteria))["ids"])
        assert python_ids == stored_ids
    legacy = index._to_store_metadata(chunks[0])
    legacy.pop("region_provenance")
    assert index._chunk_from_store(chunks[0].chunk_id, chunks[0].text, legacy).metadata["region_provenance"] == "legacy_unknown"
    event = {"service_id": "explicit", "version": 1, "event_id": "token", "action": "replace",
             "chunks": [c.to_dict() for c in chunk_service(explicit)]}
    assert handoff.validate_event(event)[0].metadata["region_provenance"] == "explicit_eligibility"


def test_stale_index_trace_is_readonly_and_distinguishes_denominators(a_storage, fake_embedder, tmp_vectorstore, monkeypatch, tmp_path):
    store.upsert_services([service("local", 소관기관명="경기도 용인시"), service("unknown", 소관기관명="중앙부처")], storage=a_storage)
    source_chunks = [c for s in store.load_services(storage=a_storage) for c in chunk_service(s)]
    index.build_index(source_chunks)
    col = index._collection()
    local_ids = [c.chunk_id for c in source_chunks if c.service_id == "local"]
    for cid in local_ids:
        col.update(ids=[cid], metadatas=[{"region": ""}])  # test setup: old codec persisted a blank
    settings = handoff.Settings.resolve(db_path=a_storage.db_path, index_dir=tmp_vectorstore)
    marker, tokens = index._read_version(), store.get_pending_changes(storage=a_storage)
    monkeypatch.setattr(index, "_collection", Mock(side_effect=AssertionError("diagnostic initialized collection")))
    traced = metadata_tools.trace(settings, ["local", "unknown"])
    assert traced["counts"]["chunks"] == len(source_chunks)
    assert traced["counts"]["services"] == 2
    assert "stale_blank_recoverable_agency_inference" in traced["rows"][0]["classifications"]
    assert "genuinely_unknown_from_available_evidence" in traced["rows"][1]["classifications"]
    assert metadata_tools.trace(replace(settings, model="missing/model"), ["local"])["index"]["state"] == "selected_collection_missing"
    absent = tmp_path / "missing-index"
    assert metadata_tools.trace(replace(settings, index_dir=str(absent)), ["local"])["index"]["state"] == "index_database_missing"
    assert not absent.exists() and store.get_pending_changes(storage=a_storage) == tokens
    assert index._read_version() == marker
    assert metadata_tools.main(["correct", "--db-path", str(a_storage.db_path), "--index-dir", str(tmp_vectorstore),
                                "--service-ids", "local", "--dry-run"]) == 0
    assert store.get_pending_changes(storage=a_storage) == tokens


def test_selected_requeue_batch_failure_replay_and_c_expansion(a_storage, fake_embedder, tmp_vectorstore, monkeypatch):
    store.upsert_services([service("A", 소관기관명="경기도 용인시", 지원내용="생활 지원", 선정기준="소득 심사", 신청방법="온라인"),
                           service("B", 소관기관명="부산광역시")], storage=a_storage)
    settings = handoff.Settings.resolve(db_path=a_storage.db_path, index_dir=tmp_vectorstore)
    assert handoff.apply_pending(settings, BIndexAdapter())["acknowledged"] == 2
    before_b = [c.to_dict() for c in index.get_service_chunks("B")]
    old = store.get_record("A", storage=a_storage)
    for c in index.get_service_chunks("A"):
        index._collection().update(ids=[c.chunk_id], metadatas=[{"region": ""}])
    produced = store.reconcile_services(["A", "missing"], storage=a_storage, today="2026-10-22")
    assert produced["queued"] == ["A"] and produced["skipped"]
    token = store.get_pending_changes(storage=a_storage)[0]
    assert token["version"] == old["version"] + 1
    assert store.get_record("A", storage=a_storage)["content_hash"] == old["content_hash"]
    assert store.get_record("A", storage=a_storage)["last_run"] == old["last_run"]
    assert store.reconcile_services(["A"], storage=a_storage, today="2026-10-22")["queued"] == ["A"]
    assert store.get_pending_changes(storage=a_storage) == [token]
    store.upsert_services([service("C")], storage=a_storage)
    selected = replace(settings, service_ids=("A",), verify_every=2)

    class Failure(BIndexAdapter):
        def verify_batch(self, *args):
            raise RuntimeError("injected_verification_failure")

    failed = handoff.apply_pending(selected, Failure())
    assert failed["acknowledged"] == 0 and failed["failed"][0]["stage"] == "verify"
    assert store.acknowledge_changes([{**token, "event_id": "stale"}], storage=a_storage) == 0
    result = handoff.apply_pending(selected, BIndexAdapter())
    assert not result["failed"] and result["acknowledged"] == 1 and result["remaining"] == 1
    assert index.get_service_chunks("A")[0].metadata["region"] == "경기"
    assert [c.to_dict() for c in index.get_service_chunks("B")] == before_b
    from src.generate import evidence
    expanded = evidence.select_evidence([index.get_service_chunks("A")[0]])
    assert len(expanded["A"]) == len(token["chunks"])
    assert all(c.metadata["region_provenance"] == "agency_inference" for c in expanded["A"])
    from src.generate import generator
    prompt = generator._build_prompt("지원 안내", expanded)
    assert "agency_inference" in prompt and "검증된 거주 자격이 아니며" in prompt
    assert {e["service_id"] for e in store.get_pending_changes(storage=a_storage)} == {"C"}


@pytest.mark.parametrize("agency,expected", [("경기도 용인시", "경기"), ("부산광역시", "부산")])
def test_recognizable_agency_blank_is_not_explained_by_A_missing_region(agency, expected):
    value = service(소관기관명=agency)
    chunk = chunk_service(value)[0]
    assert "region" not in chunk.metadata
    metadata = index._to_store_metadata(chunk)
    assert filters.agency_region(agency) == expected == metadata["region"]
    assert metadata["region_provenance"] == "agency_inference"


def test_korean_expiry_through_verified_removal(a_storage, fake_embedder, tmp_vectorstore, monkeypatch):
    monkeypatch.setattr(pipeline, "seoul_today", lambda: "2026-09-08")
    store.upsert_services([service(신청기한="2026년9월9일")], storage=a_storage)
    settings = handoff.Settings.resolve(db_path=a_storage.db_path, index_dir=tmp_vectorstore, verify_every=2)
    assert handoff.apply_pending(settings, BIndexAdapter())["acknowledged"] == 1
    col = index._collection()
    assert col.get(where=index.build_where({"today": TODAY}))["ids"]
    assert not col.get(where=index.build_where({"today": "2026-09-10"}))["ids"]
    store.reconcile_services(["A"], storage=a_storage, today=TODAY)
    assert store.get_record("A", storage=a_storage)["status"] == "active"
    assert handoff.apply_pending(settings, BIndexAdapter())["acknowledged"] == 1
    store.reconcile_services(["A"], storage=a_storage, today="2026-09-10")
    assert store.get_pending_changes(storage=a_storage)[0]["reason"] == "expired"
    removed = handoff.apply_pending(settings, BIndexAdapter())
    assert not removed["failed"] and removed["acknowledged"] == 1
    assert not index.get_service_chunks("A") and not index.bm25_search("청년", 10)


def test_explicit_correction_cli_real_worker_selected_only(a_storage, fake_embedder, tmp_vectorstore, monkeypatch):
    store.upsert_services([service("A", 소관기관명="부산광역시"), service("B")], storage=a_storage)
    settings = handoff.Settings.resolve(db_path=a_storage.db_path, index_dir=tmp_vectorstore)
    assert handoff.apply_pending(settings, BIndexAdapter())["acknowledged"] == 2
    store.upsert_services([service("B", 지원내용="unrelated pending")], storage=a_storage)
    original_run = subprocess.run

    def fake_embedding_worker(command, **kwargs):
        assert command[-1] == "src.ingest.handoff_worker"
        return original_run([sys.executable, "-m", "tests.ingest.handoff_child", "worker", "{}"], **kwargs, timeout=45)

    monkeypatch.setattr(handoff.subprocess, "run", fake_embedding_worker)
    assert metadata_tools.main(["correct", "--db-path", str(a_storage.db_path), "--index-dir", str(tmp_vectorstore),
        "--service-ids", "A", "--verify-every", "2", "--today", "2026-10-22", "--apply"]) == 0
    assert {e["service_id"] for e in store.get_pending_changes(storage=a_storage)} == {"B"}
    assert index.get_service_chunks("A")[0].metadata["region_provenance"] == "agency_inference"


def test_C_respects_explicit_region_but_preserves_soft_relaxation():
    from src.generate.generator import _drop_other_regions
    explicit = chunk_service(service("explicit", 지원대상="서울특별시 거주자", 소관기관명="부산광역시"))[0]
    other = chunk_service(service("other", 소관기관명="부산광역시"))[0]
    unknown = chunk_service(service("unknown", 소관기관명="중앙부처"))[0]
    kept, relaxed = _drop_other_regions([explicit, other, unknown], "서울특별시")
    assert [c.service_id for c in kept] == ["explicit", "unknown"] and not relaxed
    kept, relaxed = _drop_other_regions([other], "서울")
    assert kept == [other] and relaxed  # C does not always remove every mismatch


def test_failed_detail_retains_original_residence_observation_provenance(a_storage):
    def fetch(endpoint, **params):
        row = {"서비스ID": "A", "서비스명": "가구 지원"}
        if endpoint == "serviceDetail":
            row["지원대상"] = "서울특별시 거주자"
        return {"data": [row], "page": 1, "perPage": 50, "currentCount": 1, "totalCount": 1}

    first = pipeline._execute(fetch_page=fetch, storage=a_storage, per_page=50)[1]
    before = store.get_record("A", storage=a_storage)

    def failed_detail(endpoint, **params):
        if endpoint == "serviceDetail":
            from src.ingest.client import CollectionError
            raise CollectionError("http_503")
        return fetch(endpoint, **params)

    second = pipeline._execute(fetch_page=failed_detail, storage=a_storage, per_page=50)[1]
    after = store.get_record("A", storage=a_storage)
    provenance = after["payload"]["_field_provenance"]["support_target"]
    assert provenance["observation"] == "retained_value"
    assert provenance["source_acquisition"] == first["run_id"] != second["run_id"]
    assert after["payload"]["eligibility"]["residence_evidence"][0]["source_acquisition"] == first["run_id"]
    assert after["content_hash"] == before["content_hash"] and second["changed"] == 0


def test_explicit_text_supersedes_untagged_legacy_and_removal_clears_generated_region():
    value = service(지원대상="서울특별시 거주자")
    value["eligibility"] = {"region": "부산"}
    refreshed = normalize.refresh_service(value)
    assert store.build_metadata(refreshed)["region"] == "서울"
    assert store.build_metadata(refreshed)["region_provenance"] == "explicit_eligibility"
    assert refreshed["eligibility"]["legacy_region"] == {"value": "부산", "provenance": "legacy_unknown"}
    refreshed["support_target"] = "개인 가구"
    refreshed["eligibility"]["region_rule"] = "residence-lines-older"
    removed = normalize.refresh_service(refreshed)
    assert "region" not in store.build_metadata(removed)

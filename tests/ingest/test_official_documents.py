"""A-1 only: synthetic PDFs/metadata, mocked HTTP, temporary independent catalogs."""
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest
import requests
from pypdf import PdfWriter

from src.common import config
from src.ingest.document_catalog import Catalog, CatalogPaths, identity, temporal_status
from src.ingest.document_acquire import acquire, Limits
from src.ingest import official_documents as cli, document_acquire as acquisition, store
from tests.ingest.test_store_pipeline import service


def evidence():
    return {"url": "https://official.example/post", "quote": "SYNTHETIC official posting evidence",
            "location": "posting header", "verification": "official_evidence_verified", "review_status": "human_review_pending"}


def fact(value=None):
    return {"value": value, "precision": "day", "evidence": evidence()} if value else {
        "value": None, "precision": None, "unresolved_reason": "SYNTHETIC unknown date"}


def entry(sid="guide", logical="synthetic:guide", publication="synthetic-publisher:edition-identity"):
    return {"source_id": sid, "logical_identity": logical, "publication_identity": publication,
            "posting_url": evidence()["url"], "attachment_url": "https://official.example/original.pdf",
            "attachment_evidence": evidence(), "allowed_hosts": ["official.example"], "expected_format": "pdf",
            "original_filename": "SYNTHETIC-guide.pdf", "facts": {
                "title": "SYNTHETIC 2026 guide", "publisher": "SYNTHETIC publisher", "document_type": "program_guide",
                "edition_year": 2026, "publication_state": "final", "identity_evidence": evidence(),
                "posting_date": fact("2026-05-08"), "issuance_date": fact(), "effective_start": fact(), "effective_end": fact()}}


def pdf(label="SYNTHETIC-A"):
    out = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": label})
    writer.write(out)
    return out.getvalue()


class Response:
    def __init__(self, body=None, status=200, headers=None, interruption=None):
        self.body = pdf() if body is None else body
        self.status_code = status
        self.headers = {"Content-Type": "application/pdf", "Content-Length": str(len(self.body)), **(headers or {})}
        self.interruption = interruption
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size):
        yield self.body[:40]
        if self.interruption:
            raise self.interruption
        yield self.body[40:]


def session(*responses):
    result = Mock()
    result.get.side_effect = list(responses)
    return result


@pytest.fixture
def catalog(tmp_path):
    value = Catalog(CatalogPaths(tmp_path / "catalog/catalog.db", tmp_path / "originals", tmp_path / "diagnostics"))
    value.register([entry()])
    return value


def count(catalog, table):
    with sqlite3.connect(catalog.paths.db) as con:
        return con.execute("SELECT count(*) FROM " + table).fetchone()[0]


def test_idempotent_registration_dedup_and_multiple_observations(catalog):
    catalog.register([entry()])
    assert count(catalog, "catalog_audit") == 1 and count(catalog, "versions") == 0
    s = session(Response(), Response())
    first = acquire(catalog, "guide", session=s)
    again = acquire(catalog, "guide", session=s)
    assert first["version_id"] == again["version_id"] and first["attempt_id"] != again["attempt_id"]
    assert count(catalog, "artifacts") == count(catalog, "versions") == 1
    assert count(catalog, "observations") == count(catalog, "attempts") == 2
    assert first["artifact"]["quality"]["valid_pdf"] and first["artifact"]["quality"]["page_count"] == 1
    assert first["artifact"]["quality"]["extraction"] == "deferred_A2"
    assert first["artifact"]["quality"]["text_availability"] == "not_assessed"
    assert list((catalog.paths.originals / ".partial").iterdir()) == []


def test_same_url_changed_bytes_retains_both_versions_and_flags_conflict(catalog):
    a = acquire(catalog, "guide", session=session(Response()))
    b = acquire(catalog, "guide", session=session(Response(pdf("SYNTHETIC-B"))))
    assert a["version_id"] != b["version_id"] and count(catalog, "artifacts") == 2
    for row in (a, b):
        assert (catalog.paths.originals / row["artifact"]["relative_path"]).is_file()
    for vid in (a["version_id"], b["version_id"]):
        facts = entry()["facts"]; facts["effective_start"] = fact("2026-01-01")
        catalog.correct_metadata(vid, facts, reason="SYNTHETIC source-date evidence correction")
    result = catalog.inspect("2026-10-23")
    assert len(result["versions"]) == 2
    assert all(any("competing" in w for w in v["computed"]["warnings"]) for v in result["versions"])


def test_identical_bytes_do_not_merge_unrelated_legal_identities(catalog):
    catalog.register([entry("unrelated", "synthetic:different-legal-rule")])
    a = acquire(catalog, "guide", session=session(Response()))
    b = acquire(catalog, "unrelated", session=session(Response()))
    mirror = entry("observation2")
    mirror["attachment_url"] = "https://official.example/second-observed-url.pdf"
    catalog.register([mirror])
    c = acquire(catalog, "observation2", session=session(Response()))
    assert a["version_id"] != b["version_id"] and a["version_id"] == c["version_id"]
    assert count(catalog, "artifacts") == 1 and count(catalog, "versions") == 2


@pytest.mark.parametrize("response,reason", [
    (Response(b"<html>unavailable</html>"), "unexpected_html"),
    (Response(status=404), "http_404"),
    (Response(pdf()[:-15]), "truncated_pdf"),
    (Response(b"%PDF-1.7\nnot a PDF\n%%EOF"), "invalid_pdf_structure"),
    (Response(headers={"Content-Length": "999999"}), "truncated_download"),
    (Response(headers={"Content-Encoding": "gzip"}), "unexpected_content_encoding"),
    (Response(headers={"Content-Type": "text/html"}), "unexpected_html"),
    (Response(b"error message"), "unsupported_or_error_content")])
def test_failed_refresh_preserves_valid_artifact(catalog, response, reason):
    good = acquire(catalog, "guide", session=session(Response()))
    failed = acquire(catalog, "guide", session=session(response), limits=Limits(attempts=1))
    assert failed["status"] == "failed" and failed["failure"] == reason
    assert count(catalog, "artifacts") == count(catalog, "versions") == count(catalog, "observations") == 1
    assert (catalog.paths.originals / good["artifact"]["relative_path"]).exists()
    assert count(catalog, "attempts") == 2


def test_bounded_retries_timeout_rate_limit_and_exhaustion(catalog):
    sleep = Mock()
    s = session(requests.Timeout("secret URL must never be logged"), Response(status=429, headers={"Retry-After": "500"}), Response())
    result = acquire(catalog, "guide", session=s, sleep=sleep)
    assert result["status"] == "acquired_pdf" and s.get.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [1, 5]
    s = session(*[Response(status=503) for _ in range(3)])
    result = acquire(catalog, "guide", session=s, sleep=Mock())
    assert result["status"] == "failed" and s.get.call_count == 3 and count(catalog, "attempts") == 6
    assert "secret URL" not in str(catalog.inspect("2026-10-23"))


@pytest.mark.parametrize("target,reason", [
    ("https://official.example/original.pdf", "redirect_loop"),
    ("https://other.example/asset.pdf", "unverified_attachment_host"),
    ("http://official.example/asset.pdf", "io_or_validation_failure")])
def test_redirect_relationship_and_loop_bounds(catalog, target, reason):
    s = session(Response(status=302, headers={"Location": target}))
    result = acquire(catalog, "guide", session=s)
    assert result["failure"] == reason and s.get.call_count == 1


def test_verified_cross_host_relationship_not_domain_suffix(catalog):
    source = entry()
    source["attachment_url"] = "https://attachment.example/file.pdf"
    source["allowed_hosts"] = ["attachment.example"]
    source["attachment_evidence"]["quote"] = "SYNTHETIC posting explicitly links attachment.example/file.pdf"
    catalog.register([source], reason="SYNTHETIC explicitly linked host")
    assert acquire(catalog, "guide", session=session(Response()))["status"] == "acquired_pdf"
    source["attachment_evidence"]["verification"] = "candidate_unverified"
    catalog.register([source], reason="SYNTHETIC verification correction")
    s = session()
    assert acquire(catalog, "guide", session=s)["failure"] == "source_relationship_unverified"
    s.get.assert_not_called()


def test_non_pdf_and_local_original_are_not_official_pdf_claims(catalog, tmp_path):
    body = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1SYNTHETIC OLE signature only"
    nonpdf = acquire(catalog, "guide", session=session(Response(body)))
    assert nonpdf["status"] == "acquired_non_pdf" and not nonpdf["artifact"]["quality"]["valid_pdf"]
    supplied = tmp_path / "SYNTHETIC-local.pdf"; supplied.write_bytes(pdf())
    local = acquire(catalog, "guide", local_file=supplied)
    assert local["assurance"] == "unverified_local" and local["final_url"] is None
    v = next(v for v in catalog.inspect("2026-10-23")["versions"] if v["version_id"] == local["version_id"])
    assert v["artifact_assurance"] == "local_or_unverified"
    verified = acquire(catalog, "guide", session=session(Response()))
    assert verified["version_id"] == local["version_id"]


def test_interrupt_and_atomic_promotion_failure_never_publish_partial(catalog, monkeypatch):
    with pytest.raises(KeyboardInterrupt):
        acquire(catalog, "guide", session=session(Response(interruption=KeyboardInterrupt())))
    assert count(catalog, "artifacts") == 0 and not list((catalog.paths.originals / ".partial").iterdir())
    original = acquisition.os.replace
    def failed_promotion(src, dst):
        if str(dst).endswith(".pdf"): raise OSError("SYNTHETIC interrupted promotion")
        return original(src, dst)
    monkeypatch.setattr(acquisition.os, "replace", failed_promotion)
    result = acquire(catalog, "guide", session=session(Response()))
    assert result["status"] == "failed" and count(catalog, "artifacts") == count(catalog, "versions") == 0


def test_metadata_correction_is_auditable_and_does_not_replace_observation(catalog):
    result = acquire(catalog, "guide", session=session(Response()))
    before = catalog.inspect("2026-10-23")
    facts = entry()["facts"];facts["title"] = "SYNTHETIC corrected title"
    catalog.correct_metadata(result["version_id"], facts, reason="SYNTHETIC title correction")
    after = catalog.inspect("2026-10-23")
    assert before["observations"] == after["observations"]
    assert after["versions"][0]["facts"]["title"] == facts["title"]
    assert count(catalog, "catalog_audit") == 3
    catalog.correct_metadata(result["version_id"], facts, reason="SYNTHETIC identical correction")
    assert count(catalog, "catalog_audit") == 3


@pytest.mark.parametrize("start,end,state", [(None,None,"unresolved_effectivity"),
    ("2027-01-01",None,"future_effective"),("2026-01-01",None,"supported_applicable_candidate"),
    ("2025-01-01","2025-12-31","historical"),("2026-10-23","2026-10-23","supported_applicable_candidate")])
def test_asof_edition_posting_and_effectivity_are_distinct(start, end, state):
    facts=entry()["facts"];facts.update(effective_start=fact(start),effective_end=fact(end))
    assert temporal_status(facts,"2026-10-23")["state"] == state
    assert facts["posting_date"]["value"] == "2026-05-08" and facts["edition_year"] == 2026


def test_proposals_unknown_finality_and_invalid_intervals(catalog):
    source=entry();source["facts"]["publication_state"]="proposal";source["facts"]["effective_start"]=fact("2027-01-01")
    catalog.register([source])
    assert temporal_status(source["facts"],"2026-10-23")["state"]=="proposal_not_authoritative"
    source["facts"]["publication_state"]="unknown"
    state=temporal_status(source["facts"],"2026-10-23")
    assert state["timing"]=="future" and state["state"]=="unresolved_publication"
    source["facts"].update(effective_start=fact("2027-01-01"),effective_end=fact("2026-01-01"))
    with pytest.raises(ValueError,match="reversed"):
        catalog.register([source])
    source=entry();source["facts"]["identity_evidence"]["review_status"]="human_review_completed"
    with pytest.raises(ValueError,match="human_review_receipt"):
        catalog.register([source])


def test_scoped_amendment_does_not_supersede_whole_document(catalog):
    amendment=entry("amendment","synthetic:amendment","synthetic-partial-amendment-identity")
    relation={"from_identity":amendment["logical_identity"],"to_identity":entry()["logical_identity"],"kind":"amends",
              "scope":"SYNTHETIC one condition only; not full replacement","evidence":evidence()}
    catalog.register([amendment], [relation])
    for sid in ("guide","amendment"):
        acquire(catalog,sid,session=session(Response()))
    result=catalog.inspect("2026-10-23")
    assert len(result["versions"])==2 and result["relations"][0]["scope"]==relation["scope"]
    assert all(v["computed"]["state"]=="unresolved_effectivity" for v in result["versions"])


def test_many_to_many_links_require_actual_ids_identity_and_scoped_evidence(catalog,a_storage):
    store.upsert_services([service("SYN-A",서비스명="SYNTHETIC 주거급여",소관기관명="SYNTHETIC agency"),
                           service("SYN-B",서비스명="SYNTHETIC local rent",소관기관명="SYNTHETIC local")],storage=a_storage)
    doc=identity("doc",entry()["logical_identity"])
    version=acquire(catalog,"guide",session=session(Response()))["version_id"]
    catalog.link(doc,"household",evidence(),version_id=version,service_db=a_storage.db_path,service_id="SYN-A",
                 expected_name="SYNTHETIC 주거급여",expected_agency="SYNTHETIC agency")
    catalog.link(doc,"income",evidence(),version_id=version,service_db=a_storage.db_path,service_id="SYN-B",
                 expected_name="SYNTHETIC local rent",expected_agency="SYNTHETIC local")
    other=entry("common","synthetic:common");catalog.register([other])
    catalog.link(identity("doc",other["logical_identity"]),"income only",evidence(),service_db=a_storage.db_path,service_id="SYN-A",
                 expected_name="SYNTHETIC 주거급여",expected_agency="SYNTHETIC agency")
    assert count(catalog,"service_links")==3
    for sid in ("missing","SYN-B"):
        with pytest.raises(ValueError):
            catalog.link(doc,"invalid",evidence(),service_db=a_storage.db_path,service_id=sid,
                         expected_name="SYNTHETIC 주거급여",expected_agency="SYNTHETIC agency")
    unresolved=catalog.link(doc,"housing",evidence(),unresolved_reason="SYNTHETIC source DB unavailable")
    assert unresolved["state"]=="unresolved" and unresolved["review_status"]=="human_review_pending"
    with pytest.raises(ValueError,match="version_document_mismatch"):
        catalog.link(identity("doc",other["logical_identity"]),"bad version",evidence(),version_id=version,unresolved_reason="test")


def test_explicit_selection_and_readonly_missing_inspection(catalog,monkeypatch,tmp_path):
    args=["--catalog-db",str(catalog.paths.db),"--original-dir",str(catalog.paths.originals),"--diagnostics-dir",str(catalog.paths.diagnostics)]
    collect=Mock(side_effect=AssertionError("must not start network"));monkeypatch.setattr(cli,"acquire",collect)
    assert cli.main([*args,"collect","--source-ids","guide","not-registered"])==1
    assert cli.main([*args,"collect","--source-ids","guide","guide"])==1
    assert cli.main([*args,"inspect","--as-of","2026-10-23"])==0
    collect.assert_not_called()
    absent=Catalog(CatalogPaths(tmp_path/"absent.db",tmp_path/"absent-originals",tmp_path/"absent-diagnostics"))
    assert absent.inspect("2026-10-23")["state"]=="catalog_missing" and not absent.paths.db.exists()
    assert cli.inspect_services(tmp_path/"absent-services.db","주거급여")["state"]=="service_database_missing"


def test_storage_rejects_live_aliases_and_foreign_databases_before_writes(catalog,tmp_path,a_storage):
    for bad in [CatalogPaths(config.DB_PATH,tmp_path/"new1",tmp_path/"new2"),
                CatalogPaths(tmp_path/"new.db",config.RAW_DIR/"documents",tmp_path/"new2"),
                CatalogPaths(tmp_path/"new.db",tmp_path/"new1",config.VECTORSTORE_DIR/"diagnostics"),
                CatalogPaths(config.DB_PATH.parent/"alias"/".."/config.DB_PATH.name,tmp_path/"new1",tmp_path/"new2")]:
        with pytest.raises(ValueError):Catalog(bad)
    assert not (tmp_path/"new1").exists() and not (tmp_path/"new2").exists()
    store.upsert_services([service()],storage=a_storage)
    with pytest.raises(ValueError,match="not_document_catalog"):
        Catalog(CatalogPaths(a_storage.db_path,tmp_path/"new1",tmp_path/"new2"))
    with pytest.raises(ValueError,match="binding_mismatch"):
        Catalog(replace(catalog.paths,originals=tmp_path/"different-originals")).register([])


def test_existing_api_pending_and_real_index_are_untouched(catalog,a_storage,fake_embedder,tmp_vectorstore):
    from src.index import vector_store
    from src.ingest.chunker import chunk_service
    store.upsert_services([service()],storage=a_storage)
    vector_store.build_index(chunk_service(service()))
    before=store.get_pending_changes(storage=a_storage)
    documents=vector_store._collection().get()
    marker=vector_store._read_version()
    acquire(catalog,"guide",session=session(Response()))
    catalog.inspect("2026-10-23")
    assert store.get_pending_changes(storage=a_storage)==before
    assert vector_store._collection().get()==documents and vector_store._read_version()==marker


def test_committed_registry_only_records_researched_sources():
    value=json.loads(cli.REGISTRY.read_text(encoding="utf-8"))
    assert len(value["sources"])==6 and value["format_version"]==1
    for source in value["sources"]:
        assert "synthetic" not in source["logical_identity"].lower()
        assert source["facts"]["identity_evidence"]["review_status"]=="human_review_pending"
        assert source["attachment_evidence"]["url"]==source["posting_url"]
    future=next(s for s in value["sources"] if s["source_id"]=="housing-selection-2027")
    assert temporal_status(future["facts"],"2026-10-23")["timing"]=="future"
    redistribution=next(s for s in value["sources"] if s["source_id"]=="housing-selection-2026-incheon")
    primary=next(s for s in value["sources"] if s["source_id"]=="housing-selection-2026")
    assert redistribution["logical_identity"]==primary["logical_identity"]
    assert redistribution["publication_identity"]==primary["publication_identity"]
    assert redistribution["source_role"]=="official_redistribution" and redistribution["facts"]["publisher"]=="국토교통부"


def test_size_bounds_and_redirect_limit(catalog):
    oversized=acquire(catalog,"guide",session=session(Response()),limits=Limits(max_bytes=20,attempts=1))
    assert oversized["failure"]=="invalid_or_excessive_content_length"
    no_length=Response(headers={"Content-Length":"1"})
    no_length.headers.pop("Content-Length")
    streaming=acquire(catalog,"guide",session=session(no_length),limits=Limits(max_bytes=20,attempts=1))
    assert streaming["failure"]=="file_size_limit" and count(catalog,"artifacts")==0
    s=session(Response(status=302,headers={"Location":"/next"}),Response(status=302,headers={"Location":"/last"}))
    result=acquire(catalog,"guide",session=s,limits=Limits(redirects=1))
    assert result["failure"]=="redirect_limit" and s.get.call_count==2


def test_future_posting_does_not_replace_current_publication(catalog):
    current=entry();current["facts"]["effective_start"]=fact("2026-01-01")
    future=entry("future",publication="SYNTHETIC separate future issued notice")
    future["facts"].update(title="SYNTHETIC 2027",edition_year=2027,
                           posting_date=fact("2026-08-31"),effective_start=fact("2027-01-01"))
    catalog.register([current,future])
    for sid in ("guide","future"):
        acquire(catalog,sid,session=session(Response()))
    states={v["facts"]["edition_year"]:v["computed"]["state"] for v in catalog.inspect("2026-10-23")["versions"]}
    assert states=={2026:"supported_applicable_candidate",2027:"future_effective"}


def test_diagnostic_failure_after_catalog_commit_does_not_lose_original(catalog,monkeypatch):
    original=acquisition.os.replace
    def fail_summary(src,dst):
        if str(dst).endswith(".json"):raise OSError("SYNTHETIC diagnostics interruption after commit")
        return original(src,dst)
    with monkeypatch.context() as m:
        m.setattr(acquisition.os,"replace",fail_summary)
        with pytest.raises(OSError):acquire(catalog,"guide",session=session(Response()))
    assert count(catalog,"versions")==count(catalog,"observations")==1
    replay=acquire(catalog,"guide",session=session(Response()))
    assert replay["status"]=="acquired_pdf" and count(catalog,"versions")==1


def test_registration_cannot_reassign_legal_identity_or_use_url_credentials(catalog):
    source=entry(logical="SYNTHETIC other legal identity")
    with pytest.raises(ValueError,match="reassignment"):
        catalog.register([source])
    assert count(catalog,"documents")==1
    source=entry();source["attachment_url"]="https://official.example/download?serviceKey=credential"
    with pytest.raises(ValueError,match="public_https"):
        catalog.register([source])
    assert count(catalog,"sources")==1


def test_cli_local_failure_exit_and_powershell_json_bom(catalog,tmp_path):
    args=["--catalog-db",str(catalog.paths.db),"--original-dir",str(catalog.paths.originals),"--diagnostics-dir",str(catalog.paths.diagnostics)]
    registry=tmp_path/"registry.json"
    registry.write_text(json.dumps({"format_version":1,"sources":[entry()]}),encoding="utf-8-sig")
    assert cli.main([*args,"register","--registry",str(registry)])==0
    invalid=tmp_path/"error.pdf";invalid.write_bytes(b"<html>not a PDF</html>")
    assert cli.main([*args,"import-local","--source-id","guide","--file",str(invalid)])==1
    ev=tmp_path/"evidence.json";ev.write_text(json.dumps(evidence()),encoding="utf-8-sig")
    assert cli.main([*args,"link","--document-id",identity("doc",entry()["logical_identity"]),
                     "--topic","SYNTHETIC pending topic","--evidence",str(ev),"--unresolved-reason","SYNTHETIC registry missing"])==0


def test_relation_version_must_belong_to_declared_document(catalog):
    version=acquire(catalog,"guide",session=session(Response()))["version_id"]
    other=entry("other","synthetic:different-document")
    catalog.register([other])
    relation={"from_identity":other["logical_identity"],"from_version_id":version,
              "to_identity":entry()["logical_identity"],"kind":"refers-to","scope":"SYNTHETIC topic","evidence":evidence()}
    with pytest.raises(ValueError,match="relation_version_document_mismatch"):
        catalog.register([], [relation])
    assert count(catalog,"relations")==0

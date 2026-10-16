import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from benefit_rag.db import IngestionRun, Program, ProgramVersion, RawResponse, SearchDocument
from benefit_rag.gov24 import Response, SourceError
from benefit_rag.ingestion import ReplayAdapter, ingest


def count(session, table):
    return session.scalar(select(func.count()).select_from(table))


class EditedAdapter:
    def __init__(self, original, edit):
        self.original = original
        self.edit = edit

    def fetch_page(self, number, size):
        page = json.loads(self.original.fetch_page(number, size).body)
        self.edit(page)
        return Response(json.dumps(page, ensure_ascii=False).encode())


def test_idempotence_raw_history_and_evidence(engine, adapter):
    first = ingest(engine, adapter, dataset_kind="fixture")
    second = ingest(engine, adapter, dataset_kind="fixture")
    assert first["status"] == second["status"] == "completed"
    assert first["inserted"] == second["unchanged"] == 3
    with Session(engine) as session:
        assert count(session, Program) == count(session, ProgramVersion) == 3
        assert count(session, RawResponse) == 4
        assert count(session, IngestionRun) == 2
        for doc in session.scalars(select(SearchDocument)):
            version = session.get(ProgramVersion, doc.version_id)
            raw = session.get(RawResponse, version.raw_response_id)
            parts = doc.json_pointer.split("/")
            original = json.loads(raw.body)["data"][int(parts[2])][parts[3]]
            assert original[doc.start_offset : doc.end_offset] == doc.text
        program = session.scalar(select(Program).where(Program.source_record_id == "SYNTHETIC-002"))
        assert program.normalized["target_text"] is None
        assert program.normalized["eligibility_status"] == "unresolved"
        assert program.normalized["application_availability"] == "unknown"


def test_source_change_creates_revision_and_replay_keeps_kind(engine, adapter):
    ingest(engine, adapter, dataset_kind="fixture")

    def change(page):
        if page["page"] == 1:
            page["data"][0]["지원내용"] = "[합성] 변경된 상담 지원"

    report = ingest(engine, EditedAdapter(adapter, change), dataset_kind="fixture")
    assert (report["updated"], report["unchanged"]) == (1, 2)
    with Session(engine) as session:
        assert count(session, Program) == 3
        assert count(session, ProgramVersion) == 4
        program = session.scalar(select(Program).where(Program.source_record_id == "SYNTHETIC-001"))
        assert program.revision == 2 and program.index_pending
    replay = ReplayAdapter(engine, report["run_id"])
    result = ingest(engine, replay, dataset_kind=replay.dataset_kind, replay_of=report["run_id"])
    assert result["unchanged"] == 3 and result["dataset_kind"] == "fixture"


def test_volatile_metadata_does_not_create_revision(engine, adapter):
    ingest(engine, adapter, dataset_kind="fixture")

    def change(page):
        for row in page["data"]:
            row["조회수"] = 999
            row["수정일시"] = "2026-10-17 09:00:00"

    result = ingest(engine, EditedAdapter(adapter, change), dataset_kind="fixture")
    assert result["unchanged"] == 3
    with Session(engine) as session:
        assert count(session, ProgramVersion) == 3
        assert count(session, RawResponse) == 4
        assert all(
            p.normalized["source_updated_text"] == "2026-10-17 09:00:00"
            for p in session.scalars(select(Program))
        )


def test_partial_failure_does_not_delete_previously_seen_programs(engine, adapter):
    ingest(engine, adapter, dataset_kind="fixture")

    class FailedSecondPage:
        def fetch_page(self, number, size):
            if number == 2:
                raise SourceError("transport_retries_exhausted")
            return adapter.fetch_page(number, size)

    result = ingest(engine, FailedSecondPage(), dataset_kind="fixture")
    assert result["status"] == "partial"
    assert result["pages_loaded"] == 1 and result["collected_count"] == 2
    assert not result["collection_complete"]
    with Session(engine) as session:
        assert count(session, Program) == 3


@pytest.mark.parametrize("malformation", ["missing_id", "missing_name", "scalar", "duplicate"])
def test_bad_records_are_reported_without_losing_raw(engine, adapter, malformation):
    def change(page):
        if page["page"] != 1:
            return
        if malformation == "missing_id":
            page["data"][1].pop("서비스ID")
        elif malformation == "missing_name":
            page["data"][1]["서비스명"] = None
        elif malformation == "scalar":
            page["data"][1] = 17
        else:
            page["data"][1] = dict(page["data"][0])

    result = ingest(engine, EditedAdapter(adapter, change), dataset_kind="fixture")
    assert result["status"] == "partial" and result["rejected"] == 1
    with Session(engine) as session:
        assert count(session, RawResponse) == 2
        assert count(session, Program) == 2


@pytest.mark.parametrize(
    "case,code",
    [
        ("page", "pagination_mismatch"),
        ("count", "count_mismatch"),
        ("drift", "total_changed_during_collection"),
        ("empty", "premature_empty_page"),
    ],
)
def test_broken_pagination_is_not_complete(engine, adapter, case, code):
    def change(page):
        if page["page"] != 2:
            return
        if case == "page":
            page["page"] = 1
        elif case == "count":
            page["currentCount"] = 2
        elif case == "drift":
            page["totalCount"] = page["matchCount"] = 4
        else:
            page["data"] = []
            page["currentCount"] = 0

    result = ingest(engine, EditedAdapter(adapter, change), dataset_kind="fixture")
    assert result["status"] == "partial"
    assert {"code": code} in result["issues"]
    with Session(engine) as session:
        assert count(session, RawResponse) == 2
        assert count(session, Program) == 2


def test_page_limit_and_fixture_live_isolation(engine, adapter):
    result = ingest(engine, adapter, dataset_kind="fixture", max_pages=1)
    assert result["status"] == "partial" and result["collected_count"] == 2
    # 테스트 전용 namespace 검증. 실접속 성공을 뜻하지 않는다.
    ingest(engine, adapter, dataset_kind="live")
    with Session(engine) as session:
        assert count(session, Program) == 5


@pytest.mark.parametrize("response", [Response(b"not json"), Response(b"denied", 403)])
def test_failed_response_retained_before_parsing(engine, response):
    class Broken:
        def fetch_page(self, number, size):
            return response

    result = ingest(engine, Broken(), dataset_kind="fixture")
    assert result["status"] == "failed" and result["pages_received"] == 1
    with Session(engine) as session:
        raw = session.scalar(select(RawResponse))
        assert raw.body == response.body and raw.http_status == response.status


def test_database_page_rollback_keeps_raw_and_accurate_counts(engine, adapter, monkeypatch):
    import benefit_rag.ingestion as module

    original = module.load_record

    def fail_second(session, record, index, raw, dataset_kind):
        if index == 1:
            raise RuntimeError("sensitive database detail must not be reported")
        return original(session, record, index, raw, dataset_kind)

    monkeypatch.setattr(module, "load_record", fail_second)
    result = ingest(engine, adapter, dataset_kind="fixture")
    assert result["status"] == "failed"
    assert result["pages_loaded"] == result["collected_count"] == result["inserted"] == 0
    assert result["issues"] == [{"code": "ingestion_error"}]
    with Session(engine) as session:
        assert count(session, RawResponse) == 1
        assert count(session, Program) == count(session, ProgramVersion) == 0


def test_explicit_empty_collection_can_complete(engine):
    class Empty:
        def fetch_page(self, number, size):
            return Response(
                json.dumps(
                    {
                        "page": number,
                        "perPage": size,
                        "totalCount": 0,
                        "matchCount": 0,
                        "currentCount": 0,
                        "data": [],
                    }
                ).encode()
            )

    result = ingest(engine, Empty(), dataset_kind="fixture")
    assert result["status"] == "completed" and result["collected_count"] == 0
    assert result["pages_received"] == result["pages_loaded"] == 1


def test_http_auth_failure_does_not_store_request_key(engine):
    import httpx

    from benefit_rag.gov24 import Gov24Adapter

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        result = ingest(engine, Gov24Adapter(client, "test-secret-only"), dataset_kind="fixture")
    assert result["issues"] == [{"code": "http_403"}]
    with Session(engine) as session:
        raw = session.scalar(select(RawResponse))
        assert "serviceKey" not in raw.request_parameters
        assert "test-secret-only" not in json.dumps(raw.request_parameters)
        assert "?" not in raw.endpoint

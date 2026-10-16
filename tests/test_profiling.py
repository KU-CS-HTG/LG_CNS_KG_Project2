import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from benefit_rag.db import IngestionRun, Program, ProgramVersion, RawResponse
from benefit_rag.gov24 import Response, SourceError
from benefit_rag.ingestion import ingest
from benefit_rag.profiling import profile_run


class EditedAdapter:
    def __init__(self, original, edit):
        self.original = original
        self.edit = edit

    def fetch_page(self, number, size):
        page = json.loads(self.original.fetch_page(number, size).body)
        self.edit(page)
        return Response(json.dumps(page, ensure_ascii=False).encode())


def test_profile_denominators_values_and_repeatability(engine, adapter):
    report = ingest(engine, adapter, dataset_kind="fixture")
    profile = profile_run(engine, report["run_id"], dataset_kind="fixture")
    assert profile == profile_run(engine, report["run_id"], dataset_kind="fixture")
    assert profile["data_label"] == "synthetic_fixture"
    assert profile["observed"]["object_records"] == 3
    target = profile["fields"]["target_text"]
    assert (target["denominator"], target["missing_count"]) == (3, 1)
    assert target["missing_reasons"] == {"null": 1}
    assert profile["fields"]["administering_organization"]["missing_count"] == 2
    assert profile["fields"]["benefit_text"]["text_length_chars"]["count"] == 2
    assert profile["urls"]["stored_status_counts"] == {"missing": 3}
    assert profile["observed_values"]["audience_type"]["values"] == {"개인": 3}
    assert profile["observed_field_types"]["추가미확인필드"]["types"] == {"dict": 1}
    assert "geographic_coverage" not in profile["observed_values"]


def test_historical_raw_profile_does_not_follow_current_program(engine, adapter):
    first = ingest(engine, adapter, dataset_kind="fixture")
    original = profile_run(engine, first["run_id"], dataset_kind="fixture")

    def change(page):
        for record in page["data"]:
            record["서비스분야"] = "[합성] 변경된 분야"

    ingest(engine, EditedAdapter(adapter, change), dataset_kind="fixture")
    assert original == profile_run(engine, first["run_id"], dataset_kind="fixture")


def test_namespace_isolation_and_unchanged_observation_status(engine, adapter):
    first = ingest(engine, adapter, dataset_kind="fixture")
    second = ingest(engine, adapter, dataset_kind="fixture")
    # live namespace 격리 테스트이며 실제 API 호출이 아니다.
    live = ingest(engine, adapter, dataset_kind="live")
    with pytest.raises(SourceError, match="profile_namespace_mismatch"):
        profile_run(engine, first["run_id"], dataset_kind="live")
    profile = profile_run(engine, second["run_id"], dataset_kind="fixture")
    assert profile["observed"]["object_records"] == 3
    assert profile["urls"]["stored_status_counts"] == {"not_stored_for_observation": 3}
    assert profile_run(engine, live["run_id"], dataset_kind="live")["dataset_kind"] == "live"


def test_partial_and_malformed_rows_use_object_denominator(engine, adapter):
    def change(page):
        page["data"][1] = 7
        page["data"][0]["지원대상"] = ["[합성] 잘못된 타입"]

    run = ingest(engine, EditedAdapter(adapter, change), dataset_kind="fixture", max_pages=1)
    profile = profile_run(engine, run["run_id"], dataset_kind="fixture")
    assert profile["collection"]["status"] == "partial"
    assert profile["observed"]["array_entries"] == 2
    assert profile["observed"]["malformed_records"] == 1
    assert profile["fields"]["target_text"]["denominator"] == 1
    assert profile["fields"]["target_text"]["invalid_type_count"] == 1
    assert profile["fields"]["target_text"]["missing_count"] == 0


@pytest.mark.parametrize("broken", [False, True])
def test_empty_or_failed_run_and_read_only_counts(engine, broken):
    class Empty:
        def fetch_page(self, number, size):
            return Response(
                b"invalid"
                if broken
                else json.dumps(
                    {
                        "page": 1,
                        "perPage": size,
                        "totalCount": 0,
                        "matchCount": 0,
                        "currentCount": 0,
                        "data": [],
                    }
                ).encode()
            )

    run = ingest(engine, Empty(), dataset_kind="fixture")
    tables = [Program, ProgramVersion, RawResponse, IngestionRun]
    with Session(engine) as session:
        before = [session.scalar(select(func.count()).select_from(t)) for t in tables]
    profile = profile_run(engine, run["run_id"], dataset_kind="fixture")
    assert profile["observed"]["object_records"] == 0
    assert profile["observed"]["unprofiled_pages"] == ([1] if broken else [])
    for field in profile["fields"].values():
        assert field["denominator"] == field["missing_count"] == 0
        assert field["text_length_chars"]["mean"] is None
    with Session(engine) as session:
        assert before == [session.scalar(select(func.count()).select_from(t)) for t in tables]


def test_missing_and_running_runs_are_not_reported_as_frozen(engine):
    with pytest.raises(SourceError, match="run_not_found"):
        profile_run(engine, "missing", dataset_kind="fixture")
    with Session(engine, expire_on_commit=False) as session:
        run = IngestionRun(
            dataset_kind="fixture", normalization_version="test", page_size=2, max_pages=1
        )
        session.add(run)
        session.commit()
    with pytest.raises(SourceError, match="profile_run_not_finished"):
        profile_run(engine, run.id, dataset_kind="fixture")

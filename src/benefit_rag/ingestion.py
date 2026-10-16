import hashlib

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from benefit_rag.db import (
    IngestionRun,
    Program,
    ProgramVersion,
    RawResponse,
    SearchDocument,
    utcnow,
)
from benefit_rag.domain import NORMALIZATION_VERSION, DatasetKind
from benefit_rag.gov24 import (
    DOCUMENT_FIELDS,
    ENDPOINT,
    Response,
    SourceAdapter,
    SourceError,
    content_hash,
    normalize,
    parse_page,
)


class ReplayAdapter:
    def __init__(self, engine, run_id: str):
        with Session(engine) as session:
            original = session.get(IngestionRun, run_id)
            if original is None:
                raise SourceError("run_not_found")
            self.dataset_kind = original.dataset_kind
            self.page_size = original.page_size
            self.max_pages = original.max_pages
            self.responses = {
                row.page_number: Response(row.body, row.http_status)
                for row in session.scalars(select(RawResponse).where(RawResponse.run_id == run_id))
            }

    def fetch_page(self, page: int, page_size: int) -> Response:
        try:
            return self.responses[page]
        except KeyError:
            raise SourceError("replay_page_missing") from None


def load_record(
    session: Session, record: dict, index: int, raw: RawResponse, dataset_kind: str
) -> tuple[str, str]:
    data = normalize(record, index)
    digest = content_hash(record)
    program = session.scalar(
        select(Program).where(
            Program.source == "gov24",
            Program.dataset_kind == dataset_kind,
            Program.source_record_id == data.source_record_id,
        )
    )
    outcome = "inserted"
    if program is None:
        program = Program(
            source="gov24",
            dataset_kind=dataset_kind,
            source_record_id=data.source_record_id,
            revision=0,
        )
        session.add(program)
    else:
        outcome = "unchanged" if program.content_hash == digest else "updated"
    program.last_seen_at = raw.collected_at
    program.last_raw_response_id = raw.id
    program.last_record_index = index
    program.normalized = data.model_dump(mode="json")
    program.name = data.name
    if outcome == "unchanged":
        return outcome, data.source_record_id
    program.revision += 1
    program.content_hash = digest
    program.index_pending = True
    session.flush()
    version = ProgramVersion(
        program_id=program.id,
        revision=program.revision,
        content_hash=digest,
        normalization_version=NORMALIZATION_VERSION,
        normalized=data.model_dump(mode="json"),
        raw_response_id=raw.id,
        record_index=index,
        collected_at=raw.collected_at,
    )
    session.add(version)
    session.flush()
    for field in sorted(DOCUMENT_FIELDS):
        value = getattr(data, field)
        if value:
            session.add(
                SearchDocument(
                    version_id=version.id,
                    field=field,
                    text=value,
                    json_pointer=data.evidence[field].json_pointer,
                    start_offset=0,
                    end_offset=len(value),
                )
            )
    return outcome, data.source_record_id


def ingest(
    engine,
    adapter: SourceAdapter,
    *,
    dataset_kind: DatasetKind,
    page_size: int = 2,
    max_pages: int = 2,
    replay_of: str | None = None,
) -> dict:
    """한 소스/데이터 종류당 단일 writer. raw 저장 후 페이지 단위 정규화 트랜잭션."""
    if not 1 <= page_size <= 100 or not 1 <= max_pages <= 100:
        raise ValueError("page_size/max_pages must be between 1 and 100")
    run = IngestionRun(
        dataset_kind=dataset_kind,
        page_size=page_size,
        max_pages=max_pages,
        normalization_version=NORMALIZATION_VERSION,
        replay_of=replay_of,
    )
    with Session(engine, expire_on_commit=False) as session:
        session.add(run)
        session.commit()
        run_id = run.id
    seen: set[str] = set()
    expected_total = None
    complete = False
    try:
        for number in range(1, max_pages + 1):
            response = adapter.fetch_page(number, page_size)
            # 파싱 실패/HTTP 실패도 원문을 남긴다. 인증키·전체 요청 URL은 저장하지 않는다.
            with Session(engine, expire_on_commit=False) as session:
                raw = RawResponse(
                    run_id=run_id,
                    page_number=number,
                    endpoint=ENDPOINT,
                    request_parameters={"page": number, "perPage": page_size, "returnType": "JSON"},
                    http_status=response.status,
                    body=response.body,
                    sha256=hashlib.sha256(response.body).hexdigest(),
                    collected_at=utcnow(),
                )
                session.add(raw)
                current = session.get(IngestionRun, run_id)
                current.pages_received += 1
                session.commit()
            if response.status != 200:
                raise SourceError(f"http_{response.status}")
            page = parse_page(response.body, number, page_size)
            if expected_total is None:
                expected_total = page.totalCount
            elif expected_total != page.totalCount:
                raise SourceError("total_changed_during_collection")
            with Session(engine) as session, session.begin():
                current = session.get(IngestionRun, run_id)
                if current.expected_count is None:
                    current.expected_count = page.matchCount
                elif current.expected_count != page.matchCount:
                    raise SourceError("count_changed_during_collection")
                if not page.data and len(seen) < current.expected_count:
                    raise SourceError("premature_empty_page")
                for index, record in enumerate(page.data):
                    try:
                        data = normalize(record, index)
                        if data.source_record_id in seen:
                            raise SourceError("duplicate_record_in_run")
                        outcome, identifier = load_record(session, record, index, raw, dataset_kind)
                        seen.add(identifier)
                        count_field = f"{outcome}_count"
                        setattr(current, count_field, getattr(current, count_field) + 1)
                    except (SourceError, ValueError) as exc:
                        current.rejected_count += 1
                        code = str(exc) if isinstance(exc, SourceError) else "record_invalid"
                        current.issues = [
                            *current.issues,
                            {"page": number, "index": index, "code": code},
                        ]
                current.collected_count = len(seen)
                current.pages_loaded += 1
                if len(seen) > current.expected_count:
                    raise SourceError("records_exceed_count")
                if len(seen) == current.expected_count:
                    complete = current.rejected_count == 0
                    break
        if not complete:
            with Session(engine) as session, session.begin():
                current = session.get(IngestionRun, run_id)
                current.issues = [*current.issues, {"code": "collection_incomplete"}]
    except Exception as exc:
        complete = False
        # DB/transport의 상세 예외는 키나 SQL 값을 포함할 수 있으므로 저장·출력하지 않는다.
        code = (
            str(exc)
            if isinstance(exc, SourceError)
            else (
                "page_invalid"
                if isinstance(exc, (ValidationError, ValueError))
                else "ingestion_error"
            )
        )
        with Session(engine) as session, session.begin():
            current = session.get(IngestionRun, run_id)
            current.issues = [*current.issues, {"code": code}]
    with Session(engine) as session, session.begin():
        current = session.get(IngestionRun, run_id)
        current.collection_complete = complete
        current.status = (
            "completed" if complete else ("partial" if current.pages_loaded else "failed")
        )
        current.finished_at = utcnow()
        return {
            "run_id": current.id,
            "dataset_kind": current.dataset_kind,
            "status": current.status,
            "collection_complete": current.collection_complete,
            "expected_count": current.expected_count,
            "collected_count": current.collected_count,
            "pages_received": current.pages_received,
            "pages_loaded": current.pages_loaded,
            "inserted": current.inserted_count,
            "updated": current.updated_count,
            "unchanged": current.unchanged_count,
            "rejected": current.rejected_count,
            "issues": current.issues,
        }

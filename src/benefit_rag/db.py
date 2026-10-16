from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


json_type = JSON().with_variant(JSONB(), "postgresql")


class IngestionRun(Base):
    __tablename__ = "ingestion_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(80), default="gov24")
    dataset_kind: Mapped[str] = mapped_column(String(16))
    replay_of: Mapped[str | None] = mapped_column(ForeignKey("ingestion_run.id"))
    normalization_version: Mapped[str] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")
    collection_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    page_size: Mapped[int] = mapped_column(Integer)
    max_pages: Mapped[int] = mapped_column(Integer)
    pages_received: Mapped[int] = mapped_column(Integer, default=0)
    pages_loaded: Mapped[int] = mapped_column(Integer, default=0)
    expected_count: Mapped[int | None] = mapped_column(Integer)
    collected_count: Mapped[int] = mapped_column(Integer, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    issues: Mapped[list] = mapped_column(json_type, default=list)


class RawResponse(Base):
    __tablename__ = "raw_response"
    __table_args__ = (UniqueConstraint("run_id", "page_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_run.id"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    endpoint: Mapped[str] = mapped_column(Text)
    request_parameters: Mapped[dict] = mapped_column(json_type)
    http_status: Mapped[int] = mapped_column(Integer)
    body: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))


class Program(Base):
    __tablename__ = "program"
    __table_args__ = (UniqueConstraint("source", "dataset_kind", "source_record_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(80))
    dataset_kind: Mapped[str] = mapped_column(String(16))
    source_record_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    normalized: Mapped[dict] = mapped_column(json_type)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_raw_response_id: Mapped[str] = mapped_column(ForeignKey("raw_response.id"))
    last_record_index: Mapped[int] = mapped_column(Integer)
    index_pending: Mapped[bool] = mapped_column(Boolean, default=True)


class ProgramVersion(Base):
    __tablename__ = "program_version"
    __table_args__ = (UniqueConstraint("program_id", "revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    program_id: Mapped[str] = mapped_column(ForeignKey("program.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    normalization_version: Mapped[str] = mapped_column(String(80))
    normalized: Mapped[dict] = mapped_column(json_type)
    raw_response_id: Mapped[str] = mapped_column(ForeignKey("raw_response.id"))
    record_index: Mapped[int] = mapped_column(Integer)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SearchDocument(Base):
    __tablename__ = "search_document"
    __table_args__ = (UniqueConstraint("version_id", "field"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(ForeignKey("program_version.id"), index=True)
    field: Mapped[str] = mapped_column(String(80))
    text: Mapped[str] = mapped_column(Text)
    json_pointer: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer, default=0)
    end_offset: Mapped[int] = mapped_column(Integer)


def make_engine(url: str):
    engine = create_engine(url, hide_parameters=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine

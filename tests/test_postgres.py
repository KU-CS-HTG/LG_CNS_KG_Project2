import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from benefit_rag.db import Program, ProgramVersion, RawResponse, make_engine
from benefit_rag.ingestion import ingest


@pytest.mark.postgres
def test_postgres_migrations_types_and_ingestion(adapter):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("전용 개발 DB의 TEST_DATABASE_URL 필요")
    admin = make_engine(url)
    if admin.dialect.name != "postgresql":
        pytest.fail("TEST_DATABASE_URL must be PostgreSQL")
    schema = "test_benefits_" + uuid4().hex
    with admin.begin() as conn:
        conn.execute(CreateSchema(schema))
    engine = create_engine(
        url, hide_parameters=True, connect_args={"options": f"-csearch_path={schema}"}
    )
    config = Config("alembic.ini")
    try:
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            conn.commit()
            assert conn.scalar(text("SELECT current_schema()")) == schema
            assert inspect(conn).has_table("alembic_version", schema=schema)
            assert conn.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
            columns = {c["name"]: str(c["type"]) for c in inspect(conn).get_columns("program")}
            assert columns["normalized"] == "JSONB"
            assert conn.scalar(select(func.count()).select_from(Program)) == 0
        first = ingest(engine, adapter, dataset_kind="fixture")
        second = ingest(engine, adapter, dataset_kind="fixture")
        assert first["status"] == second["status"] == "completed"
        assert first["inserted"] == second["unchanged"] == 3
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(ProgramVersion)) == 3
            assert session.scalar(select(func.count()).select_from(RawResponse)) == 4
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            command.check(config)
            conn.rollback()
            command.downgrade(config, "base")
            conn.commit()
            assert not inspect(conn).has_table("program")
            conn.rollback()
            command.upgrade(config, "head")
            conn.commit()
            assert inspect(conn).has_table("program")
    finally:
        engine.dispose()
        # 이 테스트가 생성한 스키마만 SQLAlchemy 식별자 인용으로 정리한다.
        assert schema.startswith("test_benefits_") and len(schema) == 46
        with admin.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        admin.dispose()

"""initial ingestion provenance schema"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision = "fb601c5acc02"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    if op.get_context().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.create_table(
        "ingestion_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("dataset_kind", sa.String(length=16), nullable=False),
        sa.Column("replay_of", sa.String(length=36), nullable=True),
        sa.Column("normalization_version", sa.String(length=80), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("collection_complete", sa.Boolean(), nullable=False),
        sa.Column("page_size", sa.Integer(), nullable=False),
        sa.Column("max_pages", sa.Integer(), nullable=False),
        sa.Column("pages_received", sa.Integer(), nullable=False),
        sa.Column("pages_loaded", sa.Integer(), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=True),
        sa.Column("collected_count", sa.Integer(), nullable=False),
        sa.Column("inserted_count", sa.Integer(), nullable=False),
        sa.Column("updated_count", sa.Integer(), nullable=False),
        sa.Column("unchanged_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column(
            "issues",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["replay_of"],
            ["ingestion_run.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "raw_response",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column(
            "request_parameters",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_run.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "page_number"),
    )
    with op.batch_alter_table("raw_response", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_raw_response_run_id"), ["run_id"], unique=False)

    op.create_table(
        "program",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("dataset_kind", sa.String(length=16), nullable=False),
        sa.Column("source_record_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "normalized",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_raw_response_id", sa.String(length=36), nullable=False),
        sa.Column("last_record_index", sa.Integer(), nullable=False),
        sa.Column("index_pending", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["last_raw_response_id"],
            ["raw_response.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "dataset_kind", "source_record_id"),
    )
    op.create_table(
        "program_version",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("program_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("normalization_version", sa.String(length=80), nullable=False),
        sa.Column(
            "normalized",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("raw_response_id", sa.String(length=36), nullable=False),
        sa.Column("record_index", sa.Integer(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["program.id"],
        ),
        sa.ForeignKeyConstraint(
            ["raw_response_id"],
            ["raw_response.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("program_id", "revision"),
    )
    with op.batch_alter_table("program_version", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_program_version_program_id"), ["program_id"], unique=False
        )

    op.create_table(
        "search_document",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("field", sa.String(length=80), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("json_pointer", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["program_version.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "field"),
    )
    with op.batch_alter_table("search_document", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_search_document_version_id"), ["version_id"], unique=False
        )

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("search_document", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_search_document_version_id"))

    op.drop_table("search_document")
    with op.batch_alter_table("program_version", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_program_version_program_id"))

    op.drop_table("program_version")
    op.drop_table("program")
    with op.batch_alter_table("raw_response", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_raw_response_run_id"))

    op.drop_table("raw_response")
    op.drop_table("ingestion_run")
    # ### end Alembic commands ###

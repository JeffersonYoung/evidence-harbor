"""Review metadata, bounded research, and defense-in-depth immutable tables.

Revision ID: 0002
Revises: 0001
"""

import os

import sqlalchemy as sa
from alembic import op

from backend import models
from backend.db import Base

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    additions = {
        "questions": [
            ("priority", sa.String(20), "normal"),
            ("evidence_gaps", sa.JSON(), "[]"),
            ("recheck_condition", sa.Text(), ""),
        ],
        "research_runs": [("config_json", sa.JSON(), "{}"), ("usage_json", sa.JSON(), "{}")],
        "documents": [("locked_sections", sa.JSON(), "[]")],
        "proposals": [("accepted_claim_indices", sa.JSON(), None)],
        "document_versions": [("claim_ids", sa.JSON(), "[]")],
    }
    inspector = sa.inspect(bind)
    for table, columns in additions.items():
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, kind, default in columns:
            if name not in existing:
                op.add_column(table, sa.Column(name, kind, nullable=True, server_default=default))
    Base.metadata.create_all(bind=bind)
    # Tables made by an earlier build gain SQL-level protections as well.
    if bind.dialect.name == "sqlite":
        for model in models.IMMUTABLE_TABLES:
            table = model.__tablename__
            for action in ("UPDATE", "DELETE"):
                bind.execute(
                    sa.text(
                        f"CREATE TRIGGER IF NOT EXISTS no_{action.lower()}_{table} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END"
                    )
                )
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION reject_immutable_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'immutable resource: %', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql"
            )
        )
        for model in models.IMMUTABLE_TABLES:
            table = model.__tablename__
            for action in ("UPDATE", "DELETE"):
                trigger = f"no_{action.lower()}_{table}"
                bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}"))
                bind.execute(
                    sa.text(
                        f"CREATE TRIGGER {trigger} BEFORE {action} ON {table} FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation()"
                    )
                )
        if os.getenv("ENABLE_PGVECTOR", "").lower() in {"1", "true", "yes"}:
            from backend.vector_index import create_schema

            create_schema(bind)


def downgrade():
    # Review/audit history is retained deliberately; restore from backup for destructive rollback.
    pass

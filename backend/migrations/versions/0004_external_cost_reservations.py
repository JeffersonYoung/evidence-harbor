"""Explicit data classification and durable at-most-once external cost reservation.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

from backend import extensions, models  # noqa: F401 -- register mappings
from backend.db import Base

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("sources")}
    if "classification" not in existing:
        op.add_column(
            "sources", sa.Column("classification", sa.String(20), nullable=False, server_default="internal")
        )
    outbox_columns = {c["name"] for c in sa.inspect(bind).get_columns("outbox_events")}
    if "next_attempt_at" not in outbox_columns:
        op.add_column(
            "outbox_events", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
        )
    Base.metadata.create_all(bind=bind)


def downgrade():
    pass

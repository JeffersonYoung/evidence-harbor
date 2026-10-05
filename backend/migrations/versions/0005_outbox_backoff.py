"""Webhook delivery retry schedule.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("outbox_events")}
    if "next_attempt_at" not in existing:
        op.add_column(
            "outbox_events", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade():
    pass

"""Bounded research-on-source-change configuration and change audit records.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

from backend import continuous, scheduling  # noqa: F401 -- register mappings
from backend.db import Base

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("source_watches")}
    additions = [
        ("research_on_change", sa.Boolean(), sa.false()),
        ("research_question", sa.Text(), ""),
        ("research_config", sa.JSON(), "{}"),
        ("classification", sa.String(20), "internal"),
    ]
    for name, kind, default in additions:
        if name not in existing:
            op.add_column("source_watches", sa.Column(name, kind, nullable=False, server_default=default))
    Base.metadata.create_all(bind=bind)


def downgrade():
    pass

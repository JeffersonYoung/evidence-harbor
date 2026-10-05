"""New durable dispatch generation for an explicit retry of a failed operation.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

from backend import extensions, models  # noqa: F401 -- register mappings
from backend.db import Base

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("operation_dispatches")}
    if "generation" not in columns:
        op.add_column(
            "operation_dispatches", sa.Column("generation", sa.Integer(), nullable=False, server_default="0")
        )
    Base.metadata.create_all(bind=bind)


def downgrade():
    pass

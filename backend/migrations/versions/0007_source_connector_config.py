"""Scoped HTTP, RSS/Atom, and sitemap discovery configuration.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("source_watches")}
    additions = [
        ("source_type", sa.String(20), "http"),
        ("allowed_domains", sa.JSON(), "[]"),
        ("connector_state", sa.JSON(), "{}"),
        ("max_items", sa.Integer(), "50"),
    ]
    for name, kind, default in additions:
        if name not in existing:
            op.add_column("source_watches", sa.Column(name, kind, nullable=False, server_default=default))


def downgrade():
    pass

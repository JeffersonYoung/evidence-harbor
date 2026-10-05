"""Initial evidence-first workspace schema.

Revision ID: 0001
Revises: none
"""

from alembic import op

from backend import auth, continuous, extensions, models, scheduling  # noqa: F401 -- register mappings
from backend.db import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())

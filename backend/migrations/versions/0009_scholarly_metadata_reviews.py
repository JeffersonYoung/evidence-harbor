"""Append-only reviewed display corrections with CAS and retained provider evidence."""

from alembic import op

from backend.scholarly import WorkMetadataReview

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    WorkMetadataReview.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    pass

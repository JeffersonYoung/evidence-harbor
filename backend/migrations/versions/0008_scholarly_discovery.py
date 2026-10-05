"""Durable scholarly identity, provenance observations and reviewed reading links."""

from alembic import op

from backend import scholarly

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    for model in (
        scholarly.DiscoveredWork,
        scholarly.WorkAlias,
        scholarly.WorkObservation,
        scholarly.WorkReading,
    ):
        model.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    # Research identity/provenance must not be destroyed by an automatic downgrade.
    pass

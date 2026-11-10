"""Initial local OfferPilot schema."""
from alembic import op
from app.persistence import Base

revision = "0001_p0"
down_revision = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())

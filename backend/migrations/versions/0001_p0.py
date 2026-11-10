"""Initial local OfferPilot schema."""
from alembic import op
from pathlib import Path
import re

revision = "0001_p0"
down_revision = None


def upgrade():
    source = Path(__file__).parents[1] / "schema_v1.sql"
    for statement in source.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            op.get_bind().exec_driver_sql(statement)


def downgrade():
    source = Path(__file__).parents[1] / "schema_v1.sql"
    for name in reversed(re.findall(r"CREATE TABLE ([a-z_]+)", source.read_text(encoding="utf-8"))):
        op.drop_table(name)

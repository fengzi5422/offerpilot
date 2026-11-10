"""Basic manual application ownership."""
from alembic import op
from sqlalchemy import Column, ForeignKey, String, inspect
from app.persistence import Application

revision = "0002_applications"
down_revision = "0001_p0"


def upgrade():
    connection = op.get_bind()
    Application.__table__.create(connection, checkfirst=True)
    if "application_id" not in {column["name"] for column in inspect(connection).get_columns("tasks")}:
        with op.batch_alter_table("tasks") as batch:
            batch.add_column(Column("application_id", String, ForeignKey("applications.id", name="fk_task_application")))
            batch.create_index("ix_tasks_application_id", ["application_id"])


def downgrade():
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("application_id")
    op.drop_table("applications")

"""Basic manual application ownership."""
from alembic import op
from sqlalchemy import JSON, CheckConstraint, Column, ForeignKey, Integer, MetaData, String, Table, inspect

revision = "0002_applications"
down_revision = "0001_p0"


def upgrade():
    connection = op.get_bind()
    if not inspect(connection).has_table("applications"):
        op.create_table("applications", Column("company", String, nullable=False), Column("position", String),
            Column("stage", String, nullable=False), Column("source", String, nullable=False),
            Column("manual_fields", JSON, nullable=False),
            Column("closed_at", Integer), Column("id", String, primary_key=True),
            Column("created_at", Integer, nullable=False), Column("updated_at", Integer, nullable=False),
            Column("version", Integer, nullable=False))
    if "application_id" not in {column["name"] for column in inspect(connection).get_columns("tasks")}:
        with op.batch_alter_table("tasks", copy_from=tasks_table(connection)) as batch:
            batch.add_column(Column("application_id", String, ForeignKey("applications.id", name="fk_task_application")))
            batch.create_index("ix_tasks_application_id", ["application_id"])


def downgrade():
    with op.batch_alter_table("tasks", copy_from=tasks_table(op.get_bind())) as batch:
        batch.drop_index("ix_tasks_application_id")
        batch.drop_constraint("fk_task_application", type_="foreignkey")
        batch.drop_column("application_id")
    op.drop_table("applications")


def tasks_table(connection):
    table = Table("tasks", MetaData(), autoload_with=connection)
    checks = sorted((c for c in table.constraints if isinstance(c, CheckConstraint)), key=lambda c:str(c.sqltext))
    for index, constraint in enumerate(checks):
        if constraint.name is None:
            constraint.name = f"ck_tasks_v1_{index}"
    return table

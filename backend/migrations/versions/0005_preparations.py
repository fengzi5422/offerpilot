"""Versioned event preparation checklists and explicit independent reminder links."""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKey, Integer, String

revision = "0005_preparations"
down_revision = "0004_event_details"


def upgrade():
    op.create_table("preparations",
        Column("id", String, primary_key=True),
        Column("created_at", Integer, nullable=False),
        Column("updated_at", Integer, nullable=False),
        Column("version", Integer, nullable=False),
        Column("task_id", String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        Column("text", String(200), nullable=False),
        Column("completed", Boolean, nullable=False),
        Column("deleted", Boolean, nullable=False),
        Column("remind_at", Integer),
        Column("reminder_task_id", String, ForeignKey("tasks.id", ondelete="SET NULL")),
        Column("template_key", String),
        Column("parent_schedule_revision", Integer, nullable=False),
        CheckConstraint("version >= 1"))
    op.create_index("ix_preparations_task_id", "preparations", ["task_id"])
    op.create_index("uq_preparation_template", "preparations", ["task_id", "template_key"], unique=True)


def downgrade():
    op.drop_table("preparations")

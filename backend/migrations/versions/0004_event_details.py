"""Editable event location and meeting link, preserving existing task identities."""
from alembic import op
from sqlalchemy import Column, JSON, inspect

revision = "0004_event_details"
down_revision = "0003_parse_cache"


def upgrade():
    if 'event_details' not in {c['name'] for c in inspect(op.get_bind()).get_columns('tasks')}:
        op.add_column('tasks', Column('event_details', JSON, nullable=False, server_default='{}'))


def downgrade():
    with op.batch_alter_table('tasks') as batch:
        batch.drop_column('event_details')

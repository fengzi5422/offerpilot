"""Bounded temporary parser input, independent from retained evidence."""
from alembic import op
from sqlalchemy import Column, ForeignKey, Integer, String, Text, inspect, text

revision = "0003_parse_cache"
down_revision = "0002_applications"


def upgrade():
    connection = op.get_bind()
    if not inspect(connection).has_table("mail_parse_cache"):
        op.create_table("mail_parse_cache", Column("message_id", String,
            ForeignKey("email_messages.id", ondelete="CASCADE"), primary_key=True),
            Column("content", Text, nullable=False), Column("expires_at", Integer, nullable=False))
        op.create_index("ix_mail_parse_cache_expires_at", "mail_parse_cache", ["expires_at"])
    connection.execute(text("INSERT OR IGNORE INTO mail_parse_cache(message_id,content,expires_at) "
                            "SELECT id,text_excerpt,created_at+604800000 FROM email_messages "
                            "WHERE state IN ('new','parse_failed') AND text_excerpt!=''"))
    connection.execute(text("UPDATE email_messages SET text_excerpt=substr(text_excerpt,1,4096)"))


def downgrade():
    op.drop_table("mail_parse_cache")

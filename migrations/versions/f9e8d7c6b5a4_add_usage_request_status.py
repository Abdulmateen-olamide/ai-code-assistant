"""add request correlation and performance fields to usage records

Revision ID: f9e8d7c6b5a4
Revises: c4d5e6f7a8b9
Create Date: 2026-10-17 00:00:00.000000

Phase 10: enrich usage rows with correlation and performance metadata
(issue #428). All three columns are nullable so existing rows stay valid and
operations that do not report the values can leave them empty.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f9e8d7c6b5a4"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("usage_records", sa.Column("request_id", sa.String(length=64), nullable=True))
    op.add_column("usage_records", sa.Column("latency_ms", sa.Integer(), nullable=True))
    op.add_column("usage_records", sa.Column("status", sa.String(length=20), nullable=True))
    op.create_index("ix_usage_records_request_id", "usage_records", ["request_id"])
    op.create_index("ix_usage_records_status", "usage_records", ["status"])


def downgrade():
    op.drop_index("ix_usage_records_status", table_name="usage_records")
    op.drop_index("ix_usage_records_request_id", table_name="usage_records")
    op.drop_column("usage_records", "status")
    op.drop_column("usage_records", "latency_ms")
    op.drop_column("usage_records", "request_id")

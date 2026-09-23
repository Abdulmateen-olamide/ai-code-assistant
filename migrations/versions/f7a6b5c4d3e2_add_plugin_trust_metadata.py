"""add plugin trust metadata

Revision ID: f7a6b5c4d3e2
Revises: e2f3a4b5c6d7
"""

import sqlalchemy as sa
from alembic import op

revision = "f7a6b5c4d3e2"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "plugins",
        sa.Column("trust_state", sa.String(length=16), nullable=False, server_default="Unverified"),
    )
    op.add_column("plugins", sa.Column("trust_publisher", sa.String(length=256), nullable=True))


def downgrade():
    op.drop_column("plugins", "trust_publisher")
    op.drop_column("plugins", "trust_state")
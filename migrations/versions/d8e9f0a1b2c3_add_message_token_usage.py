"""add token usage to messages

Revision ID: d8e9f0a1b2c3
Revises: 9a8b7c6d5e4f
Create Date: 2026-09-27 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd8e9f0a1b2c3'
down_revision = '9a8b7c6d5e4f'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('prompt_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('completion_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('total_tokens', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.drop_column('total_tokens')
        batch_op.drop_column('completion_tokens')
        batch_op.drop_column('prompt_tokens')

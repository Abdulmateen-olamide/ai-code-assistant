"""add prompt version history

Revision ID: a1b2c3d4e5f6
Revises: f7a6b5c4d3e2
Create Date: 2026-09-23 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'f7a6b5c4d3e2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('prompt_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('prompt_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('category', sa.String(length=80), nullable=False),
    sa.Column('changed_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('prompt_deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['changed_by'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('prompt_id', 'version', name='uq_prompt_versions_prompt_version')
    )
    with op.batch_alter_table('prompt_versions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_prompt_versions_prompt_id'), ['prompt_id'], unique=False)


def downgrade():
    with op.batch_alter_table('prompt_versions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_prompt_versions_prompt_id'))

    op.drop_table('prompt_versions')

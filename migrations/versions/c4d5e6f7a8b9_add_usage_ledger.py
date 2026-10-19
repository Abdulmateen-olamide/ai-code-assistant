"""add the central usage ledger

Revision ID: c4d5e6f7a8b9
Revises: e7a8b9c0d1e2
Create Date: 2026-10-17 00:00:00.000000

Phase 10: central usage ledger model and migration (issue #366). The table is
append-only and stores metadata/token counts only; prompts and completions are
never written here.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d5e6f7a8b9'
down_revision = 'e7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'usage_records',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('workspace_id', sa.Integer(), nullable=True),
        sa.Column('project_id', sa.Integer(), nullable=True),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(length=40), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=True),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=False),
        sa.Column('completion_tokens', sa.Integer(), nullable=False),
        sa.Column('total_tokens', sa.Integer(), nullable=False),
        sa.Column('tokens_estimated', sa.Boolean(), nullable=False),
        sa.Column('cost_estimate', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_usage_records_conversation_id', 'usage_records', ['conversation_id'])
    op.create_index('ix_usage_records_created_at', 'usage_records', ['created_at'])
    op.create_index('ix_usage_records_project_id', 'usage_records', ['project_id'])
    op.create_index('ix_usage_records_source', 'usage_records', ['source'])
    op.create_index('ix_usage_records_user_id', 'usage_records', ['user_id'])
    op.create_index('ix_usage_records_workspace_id', 'usage_records', ['workspace_id'])


def downgrade():
    op.drop_index('ix_usage_records_workspace_id', table_name='usage_records')
    op.drop_index('ix_usage_records_user_id', table_name='usage_records')
    op.drop_index('ix_usage_records_source', table_name='usage_records')
    op.drop_index('ix_usage_records_project_id', table_name='usage_records')
    op.drop_index('ix_usage_records_created_at', table_name='usage_records')
    op.drop_index('ix_usage_records_conversation_id', table_name='usage_records')
    op.drop_table('usage_records')

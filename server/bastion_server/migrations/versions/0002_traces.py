"""traces table, and apps.dashboard (SDK dashboard options)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22 01:45:52.374909
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('traces',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('app_id', sa.Integer(), nullable=False),
    sa.Column('agent_id', sa.Integer(), nullable=True),
    sa.Column('agent_name', sa.String(length=100), nullable=False),
    sa.Column('flow_name', sa.String(length=100), nullable=True),
    sa.Column('app_key_id', sa.Integer(), nullable=True),
    sa.Column('trace_id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('session_id', sa.String(length=200), nullable=True),
    sa.Column('bundle_revision', sa.Integer(), nullable=True),
    sa.Column('prompt_version', sa.String(length=100), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('duration_ms', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('token_input', sa.Integer(), nullable=False),
    sa.Column('token_output', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('tool_calls', sa.JSON(), nullable=False),
    sa.Column('steps', sa.JSON(), nullable=False),
    sa.Column('summary', sa.JSON(), nullable=False),
    sa.Column('content', sa.JSON(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['app_key_id'], ['app_keys.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('app_id', 'run_id', name='uq_traces_app_run')
    )
    with op.batch_alter_table('traces', schema=None) as batch_op:
        batch_op.create_index('ix_traces_app_agent_started', ['app_id', 'agent_name', 'started_at'], unique=False)
        batch_op.create_index('ix_traces_app_session', ['app_id', 'session_id'], unique=False)
        batch_op.create_index('ix_traces_app_started', ['app_id', 'started_at'], unique=False)
        batch_op.create_index('ix_traces_app_trace', ['app_id', 'trace_id'], unique=False)
        batch_op.create_index('ix_traces_key_received', ['app_key_id', 'received_at'], unique=False)

    with op.batch_alter_table('apps', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dashboard', sa.JSON(), server_default='{}', nullable=False))



def downgrade() -> None:
    with op.batch_alter_table('apps', schema=None) as batch_op:
        batch_op.drop_column('dashboard')

    with op.batch_alter_table('traces', schema=None) as batch_op:
        batch_op.drop_index('ix_traces_key_received')
        batch_op.drop_index('ix_traces_app_trace')
        batch_op.drop_index('ix_traces_app_started')
        batch_op.drop_index('ix_traces_app_session')
        batch_op.drop_index('ix_traces_app_agent_started')

    op.drop_table('traces')

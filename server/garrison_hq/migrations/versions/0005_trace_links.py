"""traces: caller_app, parent_trace_id, chain (agent-to-agent links)

A run another app started records the calling app (from its authenticated
key), the caller's trace ID and the call chain, so the dashboard can link a
trace to its parent and to child traces in other apps.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('traces', schema=None) as batch_op:
        batch_op.add_column(sa.Column('caller_app', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('parent_trace_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('chain', sa.JSON(), server_default='[]', nullable=False))
        batch_op.create_index('ix_traces_parent_trace_id', ['parent_trace_id'])


def downgrade() -> None:
    with op.batch_alter_table('traces', schema=None) as batch_op:
        batch_op.drop_index('ix_traces_parent_trace_id')
        batch_op.drop_column('chain')
        batch_op.drop_column('parent_trace_id')
        batch_op.drop_column('caller_app')

"""traces.cost_usd may be null (no price for the model); traces.unpriced

A trace whose models have no price used to be stored with cost_usd 0.0, which
reads as free. cost_usd is now null when nothing in the trace was priced, and
`unpriced` is true when any part of it wasn't, so totals can leave those out
and say so.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('traces', schema=None) as batch_op:
        batch_op.alter_column('cost_usd', existing_type=sa.Float(), nullable=True)
        batch_op.add_column(sa.Column('unpriced', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.execute("UPDATE traces SET cost_usd = 0.0 WHERE cost_usd IS NULL")
    with op.batch_alter_table('traces', schema=None) as batch_op:
        batch_op.drop_column('unpriced')
        batch_op.alter_column('cost_usd', existing_type=sa.Float(), nullable=False)

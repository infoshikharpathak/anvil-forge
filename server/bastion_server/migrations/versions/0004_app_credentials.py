"""apps.credentials: the consumer's own credentials for granted tools

A granted tool used to be called with the granting app's auth, copied into
the consumer's bundle. Now the consumer sends a credential the granting app
issued to it: apps.credentials maps a granting app's name to "env:VAR" (a
name, never a secret), and bundles carry only the granting app's header
scheme.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps', schema=None) as batch_op:
        batch_op.add_column(sa.Column('credentials', sa.JSON(), server_default='{}', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('apps', schema=None) as batch_op:
        batch_op.drop_column('credentials')

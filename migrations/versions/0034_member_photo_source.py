"""Track the branch that owns a legacy member photo path.

Legacy rows remain NULL because their original branch cannot be inferred safely.

Revision ID: 0034_member_photo_source
Revises: 0033_backfill_spent_payments
Create Date: 2026-11-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0034_member_photo_source'
down_revision = '0033_backfill_spent_payments'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('photo_source_node_uuid', sa.String(length=36), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('photo_source_node_uuid')
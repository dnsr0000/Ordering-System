"""自動排班：員工可排時段 (staff_availability) 與每週工時上限 (staff_account.max_hours_week)

Revision ID: 0028_staff_availability
Revises: 0027_gps_carbon
Create Date: 2026-11-05 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0028_staff_availability'
down_revision = '0027_gps_carbon'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('staff_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_hours_week', sa.Integer(), nullable=False, server_default='0'))
    op.create_table(
        'staff_availability',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uuid', sa.String(length=36), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('sync_rev', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('staff_account_id', sa.Integer(), nullable=False),
        sa.Column('weekday', sa.Integer(), nullable=False),
        sa.Column('start_time', sa.String(length=5), nullable=False),
        sa.Column('end_time', sa.String(length=5), nullable=False),
        sa.Column('note', sa.String(length=100), nullable=False, server_default=''),
        sa.ForeignKeyConstraint(['staff_account_id'], ['staff_account.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('staff_availability', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_staff_availability_uuid'), ['uuid'], unique=True)
        batch_op.create_index(batch_op.f('ix_staff_availability_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_staff_availability_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_staff_availability_staff_account_id'), ['staff_account_id'], unique=False)


def downgrade():
    op.drop_table('staff_availability')
    with op.batch_alter_table('staff_account', schema=None) as batch_op:
        batch_op.drop_column('max_hours_week')

"""Add one-time owner account recovery codes.

Revision ID: 0037_tenant_recovery_code
Revises: 0036_optional_tenant_contact
Create Date: 2026-11-14 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0037_tenant_recovery_code'
down_revision = '0036_optional_tenant_contact'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'tenant_recovery_code',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('staff_account_id', sa.Integer(), nullable=False),
        sa.Column('purpose', sa.String(length=20), nullable=False),
        sa.Column('channel', sa.String(length=10), nullable=False),
        sa.Column('contact_hash', sa.String(length=64), nullable=False),
        sa.Column('code_hash', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.Column('ip_address', sa.String(length=50), nullable=False, server_default=''),
        sa.ForeignKeyConstraint(['staff_account_id'], ['staff_account.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tenant_recovery_code_tenant_id', 'tenant_recovery_code', ['tenant_id'])
    op.create_index('ix_tenant_recovery_code_staff_account_id', 'tenant_recovery_code', ['staff_account_id'])
    op.create_index('ix_tenant_recovery_code_owner_created', 'tenant_recovery_code',
                    ['tenant_id', 'staff_account_id', 'created_at'])


def downgrade():
    op.drop_index('ix_tenant_recovery_code_owner_created', table_name='tenant_recovery_code')
    op.drop_index('ix_tenant_recovery_code_staff_account_id', table_name='tenant_recovery_code')
    op.drop_index('ix_tenant_recovery_code_tenant_id', table_name='tenant_recovery_code')
    op.drop_table('tenant_recovery_code')
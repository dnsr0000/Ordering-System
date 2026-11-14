"""Track non-destructive tenant closure requests.

Revision ID: 0035_tenant_closure_request
Revises: 0034_member_photo_source
Create Date: 2026-11-14 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0035_tenant_closure_request'
down_revision = '0034_member_photo_source'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'tenant_closure_request',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('requested_by_staff_id', sa.Integer(), nullable=True),
        sa.Column('requested_by_username', sa.String(length=120), nullable=False, server_default=''),
        sa.Column('reason', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('requested_at', sa.DateTime(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_by_username', sa.String(length=120), nullable=False, server_default=''),
        sa.Column('resolution_note', sa.String(length=300), nullable=False, server_default=''),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tenant_closure_request_tenant_id', 'tenant_closure_request', ['tenant_id'])
    op.create_index('ix_tenant_closure_status_requested', 'tenant_closure_request', ['status', 'requested_at'])


def downgrade():
    op.drop_index('ix_tenant_closure_status_requested', table_name='tenant_closure_request')
    op.drop_index('ix_tenant_closure_request_tenant_id', table_name='tenant_closure_request')
    op.drop_table('tenant_closure_request')
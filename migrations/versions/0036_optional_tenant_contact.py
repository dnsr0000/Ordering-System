"""Add optional, unverified tenant contact details.

Revision ID: 0036_optional_tenant_contact
Revises: 0035_tenant_closure_request
Create Date: 2026-11-14 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0036_optional_tenant_contact'
down_revision = '0035_tenant_closure_request'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tenant', schema=None) as batch_op:
        batch_op.add_column(sa.Column('contact_email', sa.String(length=254), nullable=True))
        batch_op.add_column(sa.Column('contact_phone', sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table('tenant', schema=None) as batch_op:
        batch_op.drop_column('contact_phone')
        batch_op.drop_column('contact_email')
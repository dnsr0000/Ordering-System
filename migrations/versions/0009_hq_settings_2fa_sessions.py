"""總部統一設定 (hq_setting，只由線上端下發)、員工兩步驟驗證 (TOTP)、登入工作階段管理 (staff_session)

Revision ID: 0009_hq_settings_2fa_sessions
Revises: 0008_coupon_abuse_limits
Create Date: 2026-10-23 20:32:41.728969

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0009_hq_settings_2fa_sessions'
down_revision = '0008_coupon_abuse_limits'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('hq_setting',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=50), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('changed_by', sa.String(length=120), nullable=False),
    sa.Column('changed_at', sa.DateTime(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'key', name='uq_hq_setting_tenant_key')
    )
    with op.batch_alter_table('hq_setting', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_hq_setting_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_hq_setting_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_hq_setting_uuid'), ['uuid'], unique=True)

    op.create_table('staff_session',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('staff_account_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen', sa.DateTime(), nullable=True),
    sa.Column('ip_address', sa.String(length=50), nullable=False),
    sa.Column('user_agent', sa.String(length=300), nullable=False),
    sa.Column('device', sa.String(length=60), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_reason', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['staff_account_id'], ['staff_account.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('staff_session', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_staff_session_staff_account_id'), ['staff_account_id'], unique=False)

    with op.batch_alter_table('staff_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('totp_secret', sa.String(length=64), server_default='', nullable=False))
        batch_op.add_column(sa.Column('totp_enabled', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('totp_last_counter', sa.BigInteger(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('totp_backup_codes', sa.Text(), server_default='[]', nullable=False))
        batch_op.add_column(sa.Column('session_epoch', sa.Integer(), server_default='0', nullable=False))



def downgrade():
    with op.batch_alter_table('staff_account', schema=None) as batch_op:
        batch_op.drop_column('session_epoch')
        batch_op.drop_column('totp_backup_codes')
        batch_op.drop_column('totp_last_counter')
        batch_op.drop_column('totp_enabled')
        batch_op.drop_column('totp_secret')

    with op.batch_alter_table('staff_session', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_staff_session_staff_account_id'))

    op.drop_table('staff_session')
    with op.batch_alter_table('hq_setting', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_hq_setting_uuid'))
        batch_op.drop_index(batch_op.f('ix_hq_setting_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_hq_setting_sync_rev'))

    op.drop_table('hq_setting')

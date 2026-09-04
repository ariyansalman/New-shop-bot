"""Refer & Earn: referral codes, attribution, and the bonus amount

users gains its own code, who referred it, when that referrer was paid for
it, and a running earnings total. settings gains the bonus amount, which
defaults to 0 - Refer & Earn is off until a store decides to give money
away.

referral_rewarded_at is what makes the payout idempotent: it is stamped in
the same locked transaction as the credit, so a second completed order
finds it already set.

Revision ID: c7e83a1d4b09
Revises: b6d2f94e15c8
Create Date: 2026-09-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e83a1d4b09'
down_revision: Union[str, None] = 'b6d2f94e15c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # Skip whatever create_all() already built - see c8d5f2a41b93.
    user_cols = {c["name"] for c in inspector.get_columns("users")}
    with op.batch_alter_table('users') as batch_op:
        if 'referral_code' not in user_cols:
            batch_op.add_column(sa.Column('referral_code', sa.String(length=16), nullable=True))
            batch_op.create_index('ix_users_referral_code', ['referral_code'], unique=True)
        if 'referred_by_id' not in user_cols:
            batch_op.add_column(sa.Column('referred_by_id', sa.Integer(), nullable=True))
            batch_op.create_index('ix_users_referred_by_id', ['referred_by_id'], unique=False)
            batch_op.create_foreign_key('fk_users_referred_by', 'users',
                                        ['referred_by_id'], ['id'])
        if 'referral_rewarded_at' not in user_cols:
            batch_op.add_column(sa.Column('referral_rewarded_at', sa.DateTime(), nullable=True))
        if 'referral_earnings' not in user_cols:
            # NOT NULL with a server_default so existing rows backfill to 0
            # instead of failing the migration on a populated table.
            batch_op.add_column(sa.Column('referral_earnings', sa.Numeric(12, 2),
                                          nullable=False, server_default='0'))

    settings_cols = {c["name"] for c in inspector.get_columns("settings")}
    if 'referral_bonus' not in settings_cols:
        with op.batch_alter_table('settings') as batch_op:
            batch_op.add_column(sa.Column('referral_bonus', sa.Numeric(12, 2),
                                          nullable=False, server_default='0'))


def downgrade() -> None:
    # Balances already credited stay credited - this only removes the
    # bookkeeping, exactly as c8d5f2a41b93 does for Binance.
    with op.batch_alter_table('settings') as batch_op:
        batch_op.drop_column('referral_bonus')

    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('referral_earnings')
        batch_op.drop_column('referral_rewarded_at')
        batch_op.drop_constraint('fk_users_referred_by', type_='foreignkey')
        batch_op.drop_index('ix_users_referred_by_id')
        batch_op.drop_column('referred_by_id')
        batch_op.drop_index('ix_users_referral_code')
        batch_op.drop_column('referral_code')

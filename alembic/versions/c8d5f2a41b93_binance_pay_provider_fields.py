"""Binance Pay: payment method, verification statuses, provider fields

Revision ID: c8d5f2a41b93
Revises: b7c41a9d2e10
Create Date: 2026-09-01 12:10:00.000000

Adds everything Binance Pay needs to the EXISTING transaction system - no
second wallet, no second transaction table:

  * PaymentMethod.BINANCE_PAY
  * TransactionStatus.VERIFYING and MANUAL_REVIEW
  * transactions.provider / provider_transaction_id / verification_attempts
    / last_verification_at / last_verification_error

The UNIQUE (provider, provider_transaction_id) constraint is the
double-credit guard: one Binance transaction can be attached to exactly
one local transaction, ever. Existing CRYPTO_WALLET/CARD rows keep NULL in
both columns, and SQL treats NULLs as distinct, so they never collide with
each other.

Backend differences this has to cope with:
  * PostgreSQL stores these enums as native ENUM types, so new members
    need ALTER TYPE ... ADD VALUE.
  * SQLite stores them as plain VARCHAR with no CHECK constraint
    (SQLAlchemy 2.x defaults Enum(create_constraint=False)), so the new
    members need no DDL at all there. Verified against the real schema
    before writing this.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8d5f2a41b93'
down_revision: Union[str, None] = 'b7c41a9d2e10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (enum type name, new member) pairs, PostgreSQL only.
_NEW_ENUM_VALUES = (
    ("paymentmethod", "BINANCE_PAY"),
    ("transactionstatus", "VERIFYING"),
    ("transactionstatus", "MANUAL_REVIEW"),
)


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        # ADD VALUE is allowed inside a transaction from PostgreSQL 12 on
        # (Supabase and the CI image are both well past that). IF NOT
        # EXISTS keeps this safe to re-run.
        for type_name, value in _NEW_ENUM_VALUES:
            op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'")

    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('provider', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('provider_transaction_id', sa.String(length=128), nullable=True))
        # NOT NULL with a server_default so existing rows backfill to 0
        # instead of failing the migration on a non-empty table.
        batch_op.add_column(sa.Column('verification_attempts', sa.Integer(),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('last_verification_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('last_verification_error', sa.String(length=500), nullable=True))
        batch_op.create_index('ix_transactions_provider', ['provider'], unique=False)
        batch_op.create_unique_constraint('uq_transactions_provider_txn',
                                          ['provider', 'provider_transaction_id'])


def downgrade() -> None:
    # Remove the rows the older code could not read back. A BINANCE_PAY
    # payment_method (or a VERIFYING / MANUAL_REVIEW status) has no member
    # in the pre-Binance Python enums, so leaving these behind would make
    # the old code raise LookupError the moment it loaded one.
    #
    # This deletes payment *attempt* records only. It does not touch
    # users.wallet_balance, so money already credited by a completed
    # Binance top-up stays credited - what is lost is the audit row for
    # how it got there. That is unavoidable when removing the payment
    # method itself, and it is called out in DEPLOY.md rather than done
    # quietly.
    op.execute(sa.text(
        "DELETE FROM transactions WHERE payment_method = 'BINANCE_PAY'"
    ))
    op.execute(sa.text(
        "UPDATE transactions SET status = 'FAILED' "
        "WHERE status IN ('VERIFYING', 'MANUAL_REVIEW')"
    ))

    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.drop_constraint('uq_transactions_provider_txn', type_='unique')
        batch_op.drop_index('ix_transactions_provider')
        batch_op.drop_column('last_verification_error')
        batch_op.drop_column('last_verification_at')
        batch_op.drop_column('verification_attempts')
        batch_op.drop_column('provider_transaction_id')
        batch_op.drop_column('provider')

    # The three enum members are deliberately left in place on PostgreSQL.
    # Removing a value from an existing ENUM type is not supported without
    # rewriting the type and every column that uses it, and leaving them
    # costs nothing: the rows above are gone, so nothing references them,
    # and re-running upgrade() is a no-op thanks to IF NOT EXISTS.

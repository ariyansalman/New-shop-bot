"""Settings.binance_pay_enabled - admin kill switch for Binance top-ups

Revision ID: d3f6b18c4a27
Revises: c8d5f2a41b93
Create Date: 2026-09-01 13:50:00.000000

A boolean on the existing single-row settings table so an admin can stop
accepting Binance payments from the Telegram admin panel during an
incident without a redeploy. It only narrows the environment
configuration - it can never enable the method on a server that has no
credentials - so defaulting existing rows to true changes nothing for a
deployment that has not configured Binance at all.

server_default is required: the column is NOT NULL and the settings row
already exists in every live database, so without it the ALTER fails on
PostgreSQL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3f6b18c4a27'
down_revision: Union[str, None] = 'c8d5f2a41b93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Skip it when create_all() already built the settings table from a
    # model file that had this column - adding it twice aborts the upgrade.
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("settings")}
    if 'binance_pay_enabled' in existing:
        return

    with op.batch_alter_table('settings') as batch_op:
        batch_op.add_column(sa.Column(
            'binance_pay_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ))


def downgrade() -> None:
    with op.batch_alter_table('settings') as batch_op:
        batch_op.drop_column('binance_pay_enabled')

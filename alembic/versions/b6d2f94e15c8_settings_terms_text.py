"""settings.terms_text - the store's own rules

Shown to customers under Terms & FAQ. Nullable and empty by default: the
bot must not invent a refund or warranty policy on a store's behalf, so
the menu button stays hidden until an admin writes one.

Revision ID: b6d2f94e15c8
Revises: a3c8e5f10b76
Create Date: 2026-09-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6d2f94e15c8'
down_revision: Union[str, None] = 'a3c8e5f10b76'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Skip whatever create_all() already built - see c8d5f2a41b93.
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("settings")}
    if 'terms_text' not in existing:
        with op.batch_alter_table('settings') as batch_op:
            batch_op.add_column(sa.Column('terms_text', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('settings') as batch_op:
        batch_op.drop_column('terms_text')

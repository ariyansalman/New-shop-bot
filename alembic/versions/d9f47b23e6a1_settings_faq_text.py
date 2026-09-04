"""settings.faq_text - the FAQ page, separate from the terms

The Terms & FAQ screen offers one button for each, and a store may publish
one without the other, so they cannot share a column.

Revision ID: d9f47b23e6a1
Revises: c7e83a1d4b09
Create Date: 2026-09-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9f47b23e6a1'
down_revision: Union[str, None] = 'c7e83a1d4b09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Skip whatever create_all() already built - see c8d5f2a41b93.
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("settings")}
    if 'faq_text' not in existing:
        with op.batch_alter_table('settings') as batch_op:
            batch_op.add_column(sa.Column('faq_text', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('settings') as batch_op:
        batch_op.drop_column('faq_text')

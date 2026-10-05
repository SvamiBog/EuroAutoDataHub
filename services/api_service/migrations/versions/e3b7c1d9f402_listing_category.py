"""Категория транспорта объявления (listing.category: car | motorcycle) в индексе снятия и в v_listing

Revision ID: e3b7c1d9f402
Revises: 71711b2e6ec0
Create Date: 2026-10-04 23:30:00.000000

"""
import runpy
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e3b7c1d9f402'
down_revision: Union[str, None] = '71711b2e6ec0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _v_listing_sql() -> str:
    """v_listing ревизии 71711b2e6ec0. В ней SELECT l.*: список столбцов view фиксируется при создании,
    поэтому после нового столбца view пересоздаётся."""
    previous = runpy.run_path(str(Path(__file__).with_name("71711b2e6ec0_ml_models_dom_arbitrage.py")))
    return "CREATE VIEW v_listing AS" + previous["V_LISTING_SELECT"].format(
        extra=previous["V_LISTING_EXTRA"], join=previous["V_LISTING_JOIN"])


def upgrade() -> None:
    op.add_column('listing', sa.Column('category', sa.String(length=16), server_default='car', nullable=False))
    op.drop_index('ix_listing_scope', table_name='listing')
    op.create_index('ix_listing_scope', 'listing',
                    ['source', 'category', 'status', 'make_raw', 'model_raw', 'year'], unique=False)
    op.execute("DROP VIEW IF EXISTS v_listing")
    op.execute(_v_listing_sql())


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_listing")
    op.drop_index('ix_listing_scope', table_name='listing')
    op.create_index('ix_listing_scope', 'listing', ['source', 'status', 'make_raw', 'model_raw', 'year'],
                    unique=False)
    op.drop_column('listing', 'category')
    op.execute(_v_listing_sql())

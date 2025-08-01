"""add source_name to auto_ad table

Revision ID: f6c594b769f2
Revises: 5a299a3c0a9c
Create Date: 2025-05-29 13:12:06.109701

"""
from typing import Sequence, Union

import sqlmodel
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6c594b769f2'
down_revision: Union[str, None] = '5a299a3c0a9c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('auto_ad', sa.Column('source_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_index(op.f('ix_auto_ad_source_name'), 'auto_ad', ['source_name'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f('ix_auto_ad_source_name'), table_name='auto_ad')
    op.drop_column('auto_ad', 'source_name')
    # ### end Alembic commands ###

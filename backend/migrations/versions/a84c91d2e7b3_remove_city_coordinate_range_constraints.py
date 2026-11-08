"""Remove the virtual-grid range constraints from city coordinates.

Revision ID: a84c91d2e7b3
Revises: 0272a23d24a3
Create Date: 2026-10-25

"""
from typing import Sequence, Union

from alembic import op


revision: str = "a84c91d2e7b3"
down_revision: Union[str, None] = "0272a23d24a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_cities_top_range", "cities", type_="check")
    op.drop_constraint("ck_cities_left_range", "cities", type_="check")


def downgrade() -> None:
    op.create_check_constraint(
        "ck_cities_top_range",
        "cities",
        "top >= 0 AND top <= 1000",
    )
    op.create_check_constraint(
        "ck_cities_left_range",
        "cities",
        '"left" >= 0 AND "left" <= 1000',
    )

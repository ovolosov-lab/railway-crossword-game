"""Remove dictionary word categories.

Revision ID: d4f1a9c2e706
Revises: c13d7a2f9e41
Create Date: 2026-10-31

Downgrade restores the former schema only. Category rows and word/category
associations removed by upgrade cannot be reconstructed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d4f1a9c2e706"
down_revision: Union[str, None] = "c13d7a2f9e41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_dictionary_words_category_id_categories",
        "dictionary_words",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_dictionary_words_active_lookup",
        table_name="dictionary_words",
    )
    op.drop_column("dictionary_words", "category_id")
    op.drop_table("categories")
    op.create_index(
        "ix_dictionary_words_active_lookup",
        "dictionary_words",
        ["language_code", "letter_count"],
        unique=False,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dictionary_words_active_lookup",
        table_name="dictionary_words",
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column(
        "dictionary_words",
        sa.Column("category_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_dictionary_words_category_id_categories",
        "dictionary_words",
        "categories",
        ["category_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_dictionary_words_active_lookup",
        "dictionary_words",
        ["language_code", "category_id", "letter_count"],
        unique=False,
        postgresql_where=sa.text("is_active"),
    )

"""Add an idempotency key for individual session victories.

Revision ID: 1c24e0aa0320
Revises: be761e9d4c20
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "1c24e0aa0320"
down_revision: Union[str, None] = "be761e9d4c20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "game_results",
        sa.Column("session_id", sa.Uuid(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_game_results_session_id",
        "game_results",
        ["session_id"],
    )
    op.create_check_constraint(
        "ck_game_results_session_winner",
        "game_results",
        "session_id IS NULL OR finish_reason = 'winner'",
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires an online check for session victories.")
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE game_results IN ACCESS EXCLUSIVE MODE"))
    has_session_results = connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM game_results WHERE session_id IS NOT NULL)")
    )
    if has_session_results:
        raise RuntimeError(
            "Cannot downgrade: individual session results would lose their idempotency keys."
        )
    op.drop_constraint("ck_game_results_session_winner", "game_results", type_="check")
    op.drop_constraint("uq_game_results_session_id", "game_results", type_="unique")
    op.drop_column("game_results", "session_id")

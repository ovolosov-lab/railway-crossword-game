from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GameResult, User


@dataclass(frozen=True)
class SessionVictory:
    session_id: UUID
    winner_user_id: UUID
    started_at: datetime
    finished_at: datetime
    score_awarded: int

    def __post_init__(self) -> None:
        if self.session_id == self.winner_user_id:
            raise ValueError("A session ID must differ from its owner's user ID.")
        if self.started_at.utcoffset() is None or self.finished_at.utcoffset() is None:
            raise ValueError("Victory timestamps must include a timezone.")
        if self.finished_at < self.started_at:
            raise ValueError("A victory cannot finish before it starts.")
        if self.score_awarded < 0:
            raise ValueError("The awarded score must not be negative.")


class VictoryResultConflict(RuntimeError):
    """The stored session result disagrees with the requested victory."""


def _validate_stored_result(result: GameResult, victory: SessionVictory) -> None:
    if (
        result.winner_user_id != victory.winner_user_id
        or result.finish_reason != "winner"
        or result.started_at != victory.started_at
        or result.finished_at != victory.finished_at
        or result.score_awarded != victory.score_awarded
    ):
        logger.error("Stored victory conflicts with session {}", victory.session_id)
        raise VictoryResultConflict(
            "The stored victory does not match the requested session result."
        )


async def find_session_victory(
    session: AsyncSession,
    victory: SessionVictory,
) -> GameResult | None:
    """Reconcile a possibly committed result using a fresh primary-DB transaction."""
    result = await session.scalar(
        select(GameResult).where(GameResult.session_id == victory.session_id)
    )
    if result is not None:
        _validate_stored_result(result, victory)
    return result


async def persist_session_victory(
    session: AsyncSession,
    victory: SessionVictory,
) -> bool:
    """Commit a session victory and score together; return whether it was new.

    The caller must pass a session without an active transaction. If commit's
    outcome is unknown, discard it and reconcile with a fresh primary-DB session.
    """
    async with session.begin():
        statement = (
            insert(GameResult)
            .values(
                game_id=uuid4(),
                session_id=victory.session_id,
                winner_user_id=victory.winner_user_id,
                started_at=victory.started_at,
                finished_at=victory.finished_at,
                finish_reason="winner",
                score_awarded=victory.score_awarded,
            )
            .on_conflict_do_nothing(constraint="uq_game_results_session_id")
            .returning(GameResult.game_id)
        )
        inserted_id = await session.scalar(statement)
        if inserted_id is None:
            existing = await find_session_victory(session, victory)
            if existing is None:
                raise RuntimeError("Conflicting session victory could not be read.")
            created = False
        else:
            updated_id = await session.scalar(
                update(User)
                .where(User.id == victory.winner_user_id)
                .values(score=User.score + victory.score_awarded)
                .returning(User.id)
            )
            if updated_id is None:
                logger.error(
                    "Winner {} does not exist for session {}",
                    victory.winner_user_id,
                    victory.session_id,
                )
                raise RuntimeError("The session victory winner does not exist.")
            created = True
    return created

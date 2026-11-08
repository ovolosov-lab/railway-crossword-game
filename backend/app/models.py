from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("score >= 0", name="ck_users_score_nonnegative"),
        Index(
            "uq_users_username_lower",
            text("lower(username)"),
            unique=True,
        ),
        Index(
            "uq_users_email_lower",
            text("lower(email)"),
            unique=True,
        ),
        Index(
            "ix_users_score_desc_id",
            text("score DESC"),
            text("id"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
        server_default=func.now(),
    )

    game_results: Mapped[list[GameResult]] = relationship(
        back_populates="winner",
        passive_deletes=True,
    )


class City(Base):
    __tablename__ = "cities"
    __table_args__ = (
        CheckConstraint(
            "country_code = upper(country_code)",
            name="ck_cities_country_code_uppercase",
        ),
        Index(
            "uq_cities_name_lower",
            text("lower(name)"),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    top: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    left: Mapped[Decimal] = mapped_column("left", Numeric(6, 2), nullable=False)
    is_capital: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    outgoing_routes: Mapped[list[Route]] = relationship(
        back_populates="source_city",
        foreign_keys=lambda: [Route.source_city_id],
        passive_deletes=True,
    )
    incoming_routes: Mapped[list[Route]] = relationship(
        back_populates="target_city",
        foreign_keys=lambda: [Route.target_city_id],
        passive_deletes=True,
    )


class Route(Base):
    __tablename__ = "routes"
    __table_args__ = (
        CheckConstraint(
            "source_city_id <> target_city_id",
            name="ck_routes_distinct_cities",
        ),
        CheckConstraint(
            "length_in_letters > 0",
            name="ck_routes_length_in_letters_positive",
        ),
        Index(
            "uq_routes_unordered_city_pair",
            text("LEAST(source_city_id, target_city_id)"),
            text("GREATEST(source_city_id, target_city_id)"),
            unique=True,
        ),
        Index("ix_routes_source_city_id", "source_city_id"),
        Index("ix_routes_target_city_id", "target_city_id"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    source_city_id: Mapped[int] = mapped_column(
        ForeignKey(
            "cities.id",
            ondelete="RESTRICT",
            name="fk_routes_source_city_id_cities",
        ),
        nullable=False,
    )
    target_city_id: Mapped[int] = mapped_column(
        ForeignKey(
            "cities.id",
            ondelete="RESTRICT",
            name="fk_routes_target_city_id_cities",
        ),
        nullable=False,
    )
    length_in_letters: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    source_city: Mapped[City] = relationship(
        back_populates="outgoing_routes",
        foreign_keys=[source_city_id],
        passive_deletes=True,
    )
    target_city: Mapped[City] = relationship(
        back_populates="incoming_routes",
        foreign_keys=[target_city_id],
        passive_deletes=True,
    )


class DictionaryWord(Base):
    __tablename__ = "dictionary_words"
    __table_args__ = (
        CheckConstraint(
            "letter_count > 0",
            name="ck_dictionary_words_letter_count_positive",
        ),
        Index(
            "uq_dictionary_words_language_word",
            "language_code",
            "word_text",
            unique=True,
        ),
        Index(
            "ix_dictionary_words_active_lookup",
            "language_code",
            "letter_count",
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    word_text: Mapped[str] = mapped_column(String(100), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    language_code: Mapped[str] = mapped_column(String(5), nullable=False)
    letter_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
    )

class GameResult(Base):
    __tablename__ = "game_results"
    __table_args__ = (
        CheckConstraint(
            "finished_at >= started_at",
            name="ck_game_results_finished_after_started",
        ),
        CheckConstraint(
            "finish_reason IN ('winner', 'timeout')",
            name="ck_game_results_finish_reason",
        ),
        CheckConstraint(
            "(finish_reason = 'winner' AND winner_user_id IS NOT NULL) OR "
            "(finish_reason = 'timeout' AND winner_user_id IS NULL)",
            name="ck_game_results_winner_consistency",
        ),
        CheckConstraint(
            "score_awarded >= 0",
            name="ck_game_results_score_awarded_nonnegative",
        ),
        CheckConstraint(
            "finish_reason <> 'timeout' OR score_awarded = 0",
            name="ck_game_results_timeout_score_zero",
        ),
        Index(
            "ix_game_results_winner_finished_at",
            "winner_user_id",
            text("finished_at DESC"),
        ),
        Index(
            "ix_game_results_finished_at_desc",
            text("finished_at DESC"),
        ),
    )

    game_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )
    winner_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_game_results_winner_user_id_users",
        ),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    finish_reason: Mapped[str] = mapped_column(String(16), nullable=False)
    score_awarded: Mapped[int] = mapped_column(Integer, nullable=False)

    winner: Mapped[User | None] = relationship(back_populates="game_results")

"""Merge the latest Russian dictionary entries from the versioned catalog.

Revision ID: f4a8c21d903b
Revises: e8c42b7a19d3
Create Date: 2026-11-13

For normalized duplicate words, the last occurrence in numbered file/line order
wins. Existing definitions are backed up so downgrade can restore them.
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import SchemaItem


revision: str = "f4a8c21d903b"
down_revision: Union[str, None] = "e8c42b7a19d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WORD_FILE_PATTERNS = (
    re.compile(r"^(\d+)\.csv$"),
    re.compile(r"^кроссворд_подсказки_часть_(\d+)\.csv$"),
)
_LANGUAGE_CODE = "ru"
_BACKUP_TABLE_NAME = "dictionary_word_seed_backup_f4a8c21d903b"


def _migration_data_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _word_files() -> list[Path]:
    words_dir = _migration_data_dir() / "words"
    numbered_files: list[tuple[int, Path]] = []
    for path in words_dir.glob("*.csv"):
        match = None
        for pattern in _WORD_FILE_PATTERNS:
            match = pattern.fullmatch(path.name)
            if match is not None:
                break
        if match is None:
            raise ValueError(f"Unexpected word catalog file name: {path.name}")
        numbered_files.append((int(match.group(1)), path))

    numbered_files.sort(key=lambda item: item[0])
    numbers = [number for number, _ in numbered_files]
    if not numbers or numbers[0] < 1:
        raise ValueError("Word catalog files must use positive part numbers.")
    if len(numbers) != len(set(numbers)):
        raise ValueError("Word catalog file part numbers must be unique.")
    return [path for _, path in numbered_files]


def _normalized_duplicate_key(word_text: str) -> str:
    return unicodedata.normalize("NFKC", word_text).casefold().replace("ё", "е")


def _read_word_catalog() -> tuple[list[dict[str, object]], int]:
    rows: list[dict[str, object]] = []
    row_positions: dict[str, int] = {}
    duplicate_occurrences = 0

    for path in _word_files():
        with path.open(encoding="utf-8-sig", newline="") as source:
            for line_number, fields in enumerate(
                csv.reader(source, delimiter=";"), start=1
            ):
                if len(fields) != 2:
                    raise ValueError(
                        f"{path.name}:{line_number}: expected exactly two "
                        "semicolon-separated columns."
                    )

                raw_word, raw_definition = fields
                word_text = unicodedata.normalize("NFC", raw_word.strip())
                if not word_text:
                    raise ValueError(f"{path.name}:{line_number}: word is empty.")
                if len(word_text) > 100:
                    raise ValueError(
                        f"{path.name}:{line_number}: word exceeds 100 characters."
                    )

                definition = raw_definition.strip()
                if not definition:
                    raise ValueError(
                        f"{path.name}:{line_number}: definition is empty."
                    )

                letter_count = len(word_text)
                if not 0 < letter_count <= 32767:
                    raise ValueError(
                        f"{path.name}:{line_number}: word must contain "
                        "between 1 and 32767 letters."
                    )

                duplicate_key = _normalized_duplicate_key(word_text)
                row = {
                    "word_text": word_text,
                    "definition": definition,
                    "language_code": _LANGUAGE_CODE,
                    "letter_count": letter_count,
                }
                if duplicate_key in row_positions:
                    duplicate_occurrences += 1
                    rows[row_positions[duplicate_key]] = row
                else:
                    row_positions[duplicate_key] = len(rows)
                    rows.append(row)

    if not rows:
        raise ValueError("Word catalog contains no valid unique words.")
    return rows, duplicate_occurrences


def _validate_route_capacity_coverage(
    word_rows: list[dict[str, object]],
) -> None:
    routes_path = _migration_data_dir() / "routes.json"
    with routes_path.open(encoding="utf-8") as source:
        payload = json.load(source)

    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(routes, list) or not routes:
        raise ValueError("Route catalog must contain a non-empty routes array.")

    capacities: set[int] = set()
    for route in routes:
        if not isinstance(route, dict):
            raise ValueError("Every route catalog record must be an object.")
        capacity = route.get("length_in_letters")
        if type(capacity) is not int or capacity <= 0:
            raise ValueError(f"Invalid route word capacity: {route!r}")
        capacities.add(capacity)

    word_lengths = [int(row["letter_count"]) for row in word_rows]
    uncovered = sorted(
        capacity
        for capacity in capacities
        if not any(length <= capacity for length in word_lengths)
    )
    if uncovered:
        raise ValueError(
            "No dictionary word fits route capacities: "
            + ", ".join(map(str, uncovered))
        )


def _dictionary_words_table() -> sa.Table:
    return sa.table(
        "dictionary_words",
        sa.column("word_text", sa.String(length=100)),
        sa.column("definition", sa.Text()),
        sa.column("language_code", sa.String(length=5)),
        sa.column("letter_count", sa.SmallInteger()),
        sa.column("is_active", sa.Boolean()),
    )


def _backup_table() -> sa.Table:
    return sa.table(
        _BACKUP_TABLE_NAME,
        sa.column("language_code", sa.String(length=5)),
        sa.column("word_text", sa.String(length=100)),
        sa.column("existed", sa.Boolean()),
        sa.column("previous_definition", sa.Text()),
        sa.column("imported_definition", sa.Text()),
        sa.column("imported_letter_count", sa.SmallInteger()),
    )


def _backup_table_columns() -> list[SchemaItem]:
    return [
        sa.Column("language_code", sa.String(length=5), nullable=False),
        sa.Column("word_text", sa.String(length=100), nullable=False),
        sa.Column("existed", sa.Boolean(), nullable=False),
        sa.Column("previous_definition", sa.Text(), nullable=True),
        sa.Column("imported_definition", sa.Text(), nullable=False),
        sa.Column("imported_letter_count", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("language_code", "word_text"),
    ]


def _backup_batch(
    word_rows: list[dict[str, object]],
    dictionary_table: sa.Table,
    backup_table: sa.Table,
) -> None:
    source_words = (
        sa.values(
            sa.column("language_code", sa.String(length=5)),
            sa.column("word_text", sa.String(length=100)),
            sa.column("definition", sa.Text()),
            sa.column("letter_count", sa.SmallInteger()),
        )
        .data(
            [
                (
                    row["language_code"],
                    row["word_text"],
                    row["definition"],
                    row["letter_count"],
                )
                for row in word_rows
            ]
        )
        .cte("source_words")
    )
    existing_words = source_words.outerjoin(
        dictionary_table,
        sa.and_(
            dictionary_table.c.language_code == source_words.c.language_code,
            dictionary_table.c.word_text == source_words.c.word_text,
        ),
    )
    backup_rows = sa.select(
        source_words.c.language_code,
        source_words.c.word_text,
        dictionary_table.c.word_text.is_not(None),
        dictionary_table.c.definition,
        source_words.c.definition,
        source_words.c.letter_count,
    ).select_from(existing_words)
    op.execute(
        sa.insert(backup_table).from_select(
            [
                "language_code",
                "word_text",
                "existed",
                "previous_definition",
                "imported_definition",
                "imported_letter_count",
            ],
            backup_rows,
        )
    )


def _word_upsert_statement(
    word_rows: list[dict[str, object]],
    dictionary_table: sa.Table,
) -> sa.sql.dml.Insert:
    statement = postgresql.insert(dictionary_table).values(word_rows)
    return statement.on_conflict_do_update(
        index_elements=[
            dictionary_table.c.language_code,
            dictionary_table.c.word_text,
        ],
        set_={"definition": statement.excluded.definition},
    )


def upgrade() -> None:
    word_rows, _ = _read_word_catalog()
    _validate_route_capacity_coverage(word_rows)

    dictionary_table = _dictionary_words_table()
    backup_table = _backup_table()
    op.create_table(_BACKUP_TABLE_NAME, *_backup_table_columns())
    op.execute(sa.text("LOCK TABLE dictionary_words IN SHARE ROW EXCLUSIVE MODE"))
    for offset in range(0, len(word_rows), 500):
        batch = word_rows[offset : offset + 500]
        _backup_batch(batch, dictionary_table, backup_table)
        op.execute(_word_upsert_statement(batch, dictionary_table))


def downgrade() -> None:
    table = _dictionary_words_table()
    backup = _backup_table()
    match = sa.and_(
        backup.c.language_code == table.c.language_code,
        backup.c.word_text == table.c.word_text,
    )
    op.get_bind().execute(
        sa.update(table)
        .where(
            match,
            backup.c.existed.is_(True),
            table.c.definition == backup.c.imported_definition,
        )
        .values(definition=backup.c.previous_definition)
    )
    op.get_bind().execute(
        sa.delete(table).where(
            sa.exists(
                sa.select(1)
                .select_from(backup)
                .where(
                    match,
                    backup.c.existed.is_(False),
                    table.c.definition == backup.c.imported_definition,
                    table.c.letter_count == backup.c.imported_letter_count,
                    table.c.is_active.is_(True),
                )
            )
        )
    )
    op.drop_table(_BACKUP_TABLE_NAME)

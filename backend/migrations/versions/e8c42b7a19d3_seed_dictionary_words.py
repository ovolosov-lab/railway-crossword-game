"""Seed the Russian dictionary from the versioned crossword catalog.

Revision ID: e8c42b7a19d3
Revises: d4f1a9c2e706
Create Date: 2026-10-31

Duplicate words are compared case-insensitively after Unicode normalization
and mapping ё to е. The first occurrence in numbered file/line order wins.
Downgrade removes the imported rows; it does not restore prior dictionary
contents, so upgrade requires an empty dictionary_words table. Keep the CSV
files immutable after this revision is applied; later catalog changes require
a new migration.
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "e8c42b7a19d3"
down_revision: Union[str, None] = "d4f1a9c2e706"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WORD_FILE_PATTERNS = (
    re.compile(r"^(\d+)\.csv$"),
    re.compile(r"^кроссворд_подсказки_часть_(\d+)\.csv$"),
)
_LANGUAGE_CODE = "ru"


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


def _letter_count(word_text: str) -> int:
    return len(word_text)


def _read_word_catalog() -> tuple[list[dict[str, object]], int]:
    rows: list[dict[str, object]] = []
    seen_words: set[str] = set()
    skipped_duplicates = 0

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

                duplicate_key = _normalized_duplicate_key(word_text)
                if duplicate_key in seen_words:
                    skipped_duplicates += 1
                    continue

                definition = raw_definition.strip()
                if not definition:
                    raise ValueError(
                        f"{path.name}:{line_number}: definition is empty."
                    )

                letter_count = _letter_count(word_text)
                if not 0 < letter_count <= 32767:
                    raise ValueError(
                        f"{path.name}:{line_number}: word must contain "
                        "between 1 and 32767 letters."
                    )

                seen_words.add(duplicate_key)
                rows.append(
                    {
                        "word_text": word_text,
                        "definition": definition,
                        "language_code": _LANGUAGE_CODE,
                        "letter_count": letter_count,
                    }
                )

    if not rows:
        raise ValueError("Word catalog contains no valid unique words.")
    return rows, skipped_duplicates


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
    )


def upgrade() -> None:
    word_rows, _ = _read_word_catalog()
    _validate_route_capacity_coverage(word_rows)

    if not context.is_offline_mode():
        existing_count = op.get_bind().execute(
            sa.text("SELECT count(*) FROM dictionary_words")
        ).scalar_one()
        if existing_count:
            raise RuntimeError(
                "Dictionary seed migration requires an empty dictionary_words "
                f"table; found {existing_count} existing rows."
            )

    op.bulk_insert(_dictionary_words_table(), word_rows)


def downgrade() -> None:
    word_rows, _ = _read_word_catalog()
    table = _dictionary_words_table()
    inserted_values = [
        (
            row["language_code"],
            row["word_text"],
            row["definition"],
            row["letter_count"],
        )
        for row in word_rows
    ]
    op.get_bind().execute(
        sa.delete(table).where(
            sa.tuple_(
                table.c.language_code,
                table.c.word_text,
                table.c.definition,
                table.c.letter_count,
            ).in_(inserted_values)
        )
    )

"""Import English dictionary files 5 through 11.

Revision ID: ab3d7f51c9e2
Revises: 72fd40c93b31

The preceding English dictionary revision imports files 1 through 4. This
revision adds the remaining frozen catalog files without changing existing
dictionary entries. Duplicate (language_code, word_text) rows are skipped.
"""
from __future__ import annotations

import csv
import hashlib
import io
import unicodedata
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.selectable import TableClause


revision: str = "ab3d7f51c9e2"
down_revision: Union[str, None] = "72fd40c93b31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FILES = {
    "5.csv": "9c49ec2d74e4d3d565d445a513b26ace7250a5601e64458c063bf97f468da712",
    "6.csv": "4c6d288706cdcf6934383c3859a7918077c51d3c9770ec99b86303402a0aae55",
    "7.csv": "260b86adf09ff852a4a7eb0efc382aa97e5d77cf1e041a1e6266e168e12ab5de",
    "8.csv": "e0103dbccb1fc24b0ded797d40f6eafc5043fccfa6805aa639d898ee2af823e4",
    "9.csv": "a03fba7bddd81cd8c09d3053e1b39ed4a32f6d039beb2e11f2f08a40224f4ff4",
    "10.csv": "1222eb1a671fbd90778ff5cadf5650f3ed29466e604e371f50753ed313b72a03",
    "11.csv": "faaf8bb572379367dc205a66affb403c55dda31d0abd5678b6bbc0f2083305c6",
}
_BACKUP_TABLE = "dictionary_word_seed_backup_ab3d7f51c9e2"


def _read_catalog() -> list[dict[str, object]]:
    words_dir = Path(__file__).resolve().parents[1] / "words" / "en"
    rows: list[dict[str, object]] = []
    for filename, expected_digest in _FILES.items():
        path = words_dir / filename
        data = path.read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(data).hexdigest() != expected_digest:
            raise ValueError(f"English catalog file has changed: {filename}")
        source = io.StringIO(data.decode("utf-8-sig"), newline="")
        reader = csv.reader(source, delimiter=";", strict=True)
        for fields in reader:
            if len(fields) != 2:
                raise ValueError(
                    f"{filename}:{reader.line_num}: expected two columns"
                )
            word = unicodedata.normalize("NFC", fields[0].strip())
            definition = fields[1].strip()
            if not word or len(word) > 100 or not any(
                unicodedata.category(char).startswith("L") for char in word
            ):
                raise ValueError(f"{filename}:{reader.line_num}: invalid word")
            if not definition:
                raise ValueError(f"{filename}:{reader.line_num}: empty definition")
            rows.append(
                {
                    "word_text": word,
                    "definition": definition,
                    "language_code": "en",
                    "letter_count": len(word),
                }
            )
    if not rows:
        raise ValueError("English dictionary files 5 through 11 are empty")
    return rows


def _tables() -> tuple[TableClause, TableClause]:
    words = sa.table(
        "dictionary_words",
        sa.column("id", sa.Integer),
        sa.column("word_text", sa.String(100)),
        sa.column("definition", sa.Text),
        sa.column("language_code", sa.String(5)),
        sa.column("letter_count", sa.SmallInteger),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    backup = sa.table(
        _BACKUP_TABLE,
        sa.column("id", sa.Integer),
        sa.column("word_text", sa.String(100)),
        sa.column("definition", sa.Text),
        sa.column("language_code", sa.String(5)),
        sa.column("letter_count", sa.SmallInteger),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    return words, backup


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("English dictionary migration requires an online database")

    catalog = _read_catalog()
    words, backup = _tables()
    op.create_table(
        _BACKUP_TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("word_text", sa.String(100), nullable=False),
        sa.Column("definition", sa.Text, nullable=False),
        sa.Column("language_code", sa.String(5), nullable=False),
        sa.Column("letter_count", sa.SmallInteger, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE dictionary_words IN SHARE ROW EXCLUSIVE MODE"))
    for offset in range(0, len(catalog), 500):
        batch = catalog[offset : offset + 500]
        statement = postgresql.insert(words).values(batch).on_conflict_do_nothing(
            index_elements=[words.c.language_code, words.c.word_text]
        )
        inserted = connection.execute(
            statement.returning(
                words.c.id,
                words.c.word_text,
                words.c.definition,
                words.c.language_code,
                words.c.letter_count,
                words.c.is_active,
                words.c.created_at,
            )
        ).mappings()
        snapshots = [dict(row) for row in inserted]
        if snapshots:
            connection.execute(sa.insert(backup).values(snapshots))


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("English dictionary downgrade requires an online database")

    words, backup = _tables()
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE dictionary_words IN SHARE ROW EXCLUSIVE MODE"))
    snapshots = connection.execute(sa.select(backup)).mappings().all()
    if snapshots:
        current = {
            row["id"]: row
            for row in connection.execute(
                sa.select(words).where(
                    words.c.id.in_([row["id"] for row in snapshots])
                )
            ).mappings()
        }
        checked_columns = (
            "word_text",
            "definition",
            "language_code",
            "letter_count",
            "is_active",
            "created_at",
        )
        for snapshot in snapshots:
            row = current.get(snapshot["id"])
            if row is None or any(row[column] != snapshot[column] for column in checked_columns):
                raise RuntimeError(
                    f"Cannot downgrade: English dictionary word {snapshot['word_text']!r} "
                    "was changed or removed"
                )
        for snapshot in snapshots:
            connection.execute(sa.delete(words).where(words.c.id == snapshot["id"]))
    op.drop_table(_BACKUP_TABLE)

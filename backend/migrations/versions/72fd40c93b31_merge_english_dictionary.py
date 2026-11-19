"""Merge a frozen English catalog without changing existing Russian words.

Revision ID: 72fd40c93b31
Revises: 1c24e0aa0320
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import unicodedata
from pathlib import Path
from typing import Sequence, TypedDict, Union

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.sql.selectable import TableClause


revision: str = "72fd40c93b31"
down_revision: Union[str, None] = "1c24e0aa0320"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FILES = {
    "1.csv": "f0a75cc33420e5ae5422cea06989ce4b210d8e44a9b4e1e64dee1a47b151707c",
    "2.csv": "607b400dbe4623347abfe2179991935e62aed1e5a1ffbdc0ff745cf6b7077182",
    "3.csv": "bc79af4e8cd32310c29f94a7ad85a71f76d4c6b2bf2be83ac892498b28bc50dc",
    "4.csv": "e176d1f92fddd86bb20af23de347a8ef2123219bd80fa2607d2905a5a8e166d9",
}
_BACKUP = "dictionary_word_seed_backup_72fd40c93b31"
_WHITE_SPACE = frozenset(
    "\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
_DASHES = frozenset("-\u2010\u2011\u2012\u2013\u2014\u2015")


class CatalogEntry(TypedDict):
    word_text: str
    definition: str
    letter_count: int


def _key(word: str) -> str:
    word = unicodedata.normalize("NFC", word).lower().replace("ё", "е")
    return "".join(char for char in word if char not in _WHITE_SPACE | _DASHES)


def _read_catalog() -> list[CatalogEntry]:
    root = Path(__file__).resolve().parents[1]
    rows: dict[str, CatalogEntry] = {}
    for name, digest in _FILES.items():
        path = root / "words" / "en" / name
        data = path.read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"English catalog file has changed: {name}")
        source = io.StringIO(data.decode("utf-8-sig"), newline="")
        reader = csv.reader(source, delimiter=";", strict=True)
        for fields in reader:
            if len(fields) != 2:
                raise ValueError(f"{name}:{reader.line_num}: expected two columns")
            word = unicodedata.normalize("NFC", fields[0].strip())
            definition = fields[1].strip()
            if not word or len(word) > 100 or not any(
                unicodedata.category(char).startswith("L") for char in word
            ):
                raise ValueError(f"{name}:{reader.line_num}: invalid word")
            if not definition:
                raise ValueError(f"{name}:{reader.line_num}: empty definition")
            rows[_key(word)] = {
                "word_text": word,
                "definition": definition,
                "letter_count": len(word),
            }
    if not rows:
        raise ValueError("English catalog is empty")
    with (root / "routes.json").open(encoding="utf-8") as source:
        routes = json.load(source)["routes"]
    if not routes:
        raise ValueError("Route catalog is empty")
    lengths = [row["letter_count"] for row in rows.values()]
    for route in routes:
        capacity = route["length_in_letters"] - 2
        if capacity >= 3 and not any(3 <= length <= capacity for length in lengths):
            raise ValueError(f"No English word fits route capacity {capacity}")
    return list(rows.values())


def _tables() -> tuple[TableClause, TableClause]:
    words = sa.table(
        "dictionary_words",
        sa.column("id", sa.Integer),
        sa.column("language_code", sa.String(5)),
        sa.column("word_text", sa.String(100)),
        sa.column("definition", sa.Text),
        sa.column("letter_count", sa.SmallInteger),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    backup = sa.table(
        _BACKUP,
        sa.column("id", sa.Integer),
        sa.column("existed", sa.Boolean),
        sa.column("previous_definition", sa.Text),
        sa.column("word_text", sa.String(100)),
        sa.column("definition", sa.Text),
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
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE dictionary_words IN SHARE ROW EXCLUSIVE MODE"))
    existing: dict[str, list[sa.RowMapping]] = {}
    for row in connection.execute(
        sa.select(words).where(words.c.language_code == "en")
    ).mappings():
        existing.setdefault(_key(row["word_text"]), []).append(row)
    for entry in catalog:
        key = _key(str(entry["word_text"]))
        if len(existing.get(key, [])) > 1:
            raise RuntimeError(f"Ambiguous existing English dictionary word: {entry['word_text']!r}")

    op.create_table(
        _BACKUP,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("existed", sa.Boolean, nullable=False),
        sa.Column("previous_definition", sa.Text),
        sa.Column("word_text", sa.String(100), nullable=False),
        sa.Column("definition", sa.Text, nullable=False),
        sa.Column("letter_count", sa.SmallInteger, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    snapshots = []
    for entry in catalog:
        matches = existing.get(_key(str(entry["word_text"])), [])
        if matches:
            previous = matches[0]
            row = connection.execute(
                sa.update(words)
                .where(words.c.id == previous["id"])
                .values(definition=entry["definition"])
                .returning(words)
            ).mappings().one()
            previous_definition = previous["definition"]
        else:
            row = connection.execute(
                sa.insert(words)
                .values(**entry, language_code="en")
                .returning(words)
            ).mappings().one()
            previous_definition = None
        snapshots.append({
            "id": row["id"],
            "existed": bool(matches),
            "previous_definition": previous_definition,
            "word_text": row["word_text"],
            "definition": row["definition"],
            "letter_count": row["letter_count"],
            "is_active": row["is_active"],
            "created_at": row["created_at"],
        })
    connection.execute(sa.insert(backup), snapshots)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("English dictionary downgrade requires an online database")
    words, backup = _tables()
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE dictionary_words IN SHARE ROW EXCLUSIVE MODE"))
    saved = connection.execute(sa.select(backup)).mappings().all()
    current = {
        row["id"]: row
        for row in connection.execute(
            sa.select(words).where(words.c.id.in_([row["id"] for row in saved]))
        ).mappings()
    }
    for row in saved:
        word = current.get(row["id"])
        if word is None or word["language_code"] != "en" or any(
            word[field] != row[field]
            for field in ("word_text", "definition", "letter_count", "is_active", "created_at")
        ):
            raise RuntimeError(f"Cannot downgrade: dictionary word {row['id']} was changed or removed")
    for row in saved:
        if row["existed"]:
            connection.execute(
                sa.update(words)
                .where(words.c.id == row["id"])
                .values(definition=row["previous_definition"])
            )
        else:
            connection.execute(sa.delete(words).where(words.c.id == row["id"]))
    op.drop_table(_BACKUP)

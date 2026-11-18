"""Localize city names while keeping routes linked to physical cities.

Revision ID: be761e9d4c20
Revises: f4a8c21d903b
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Sequence, TypedDict, Union

import sqlalchemy as sa
from alembic import op


revision: str = "be761e9d4c20"
down_revision: Union[str, None] = "f4a8c21d903b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_ROUTE_PAIRS = ((15, 34), (12, 37), (15, 37))
_CITY_METADATA = ("country_code", "top", "left", "is_capital")


class CitySeed(TypedDict):
    id: int
    lang: str
    name: str
    country_code: str
    top: float
    left: float
    is_capital: bool


def _read_city_catalogs() -> dict[str, dict[int, CitySeed]]:
    directory = Path(__file__).resolve().parents[1]
    catalogs: dict[str, dict[int, CitySeed]] = {}
    for lang in ("ru", "en"):
        with (directory / f"cities_{lang}.json").open(encoding="utf-8") as source:
            payload = json.load(source)
        if not isinstance(payload, dict) or not isinstance(payload.get("cities"), list):
            raise ValueError(f"Invalid {lang} city catalog.")
        rows: dict[int, CitySeed] = {}
        names: set[str] = set()
        for row in payload["cities"]:
            if not isinstance(row, dict):
                raise ValueError(f"Invalid {lang} city record.")
            city_id, name = row.get("id"), row.get("name")
            country, top, left, capital = (
                row.get("country_code"), row.get("top"), row.get("left"),
                row.get("is_capital"),
            )
            if (
                not isinstance(city_id, int) or isinstance(city_id, bool)
                or city_id <= 0 or city_id in rows
                or not isinstance(name, str) or not 0 < len(name.strip()) <= 120
                or name.strip().casefold() in names
                or not isinstance(country, str) or re.fullmatch(r"[A-Z]{2}", country) is None
                or not isinstance(top, (int, float)) or isinstance(top, bool) or not math.isfinite(top)
                or not 0 <= top <= 3000
                or not isinstance(left, (int, float)) or isinstance(left, bool) or not math.isfinite(left)
                or not 0 <= left <= 4000
                or not isinstance(capital, bool)
            ):
                raise ValueError(f"Invalid or duplicate {lang} city: {row!r}")
            names.add(name.strip().casefold())
            rows[city_id] = {
                "id": city_id, "lang": lang, "name": name.strip(), "country_code": country,
                "top": float(top), "left": float(left), "is_capital": capital,
            }
        if not rows:
            raise ValueError(f"The {lang} city catalog is empty.")
        catalogs[lang] = rows

    ru, en = catalogs["ru"], catalogs["en"]
    if ru.keys() != en.keys():
        raise ValueError("Russian and English city IDs must match.")
    for city_id in ru:
        if any(ru[city_id][key] != en[city_id][key] for key in _CITY_METADATA):
            raise ValueError(f"City metadata differs between languages for ID {city_id}.")
    return catalogs


def _new_routes(cities: dict[int, CitySeed]) -> list[dict[str, int]]:
    routes = []
    for source, target in _NEW_ROUTE_PAIRS:
        a, b = cities[source], cities[target]
        length = math.floor(
            math.hypot(a["left"] - b["left"], a["top"] - b["top"])
            / 26
        )
        if not 0 < length <= 32767:
            raise ValueError(f"Invalid calculated route length for {source}, {target}.")
        routes.append({"source": source, "target": target, "length": length})
    return routes


def upgrade() -> None:
    catalogs = _read_city_catalogs()
    routes = _new_routes(catalogs["ru"])
    connection = op.get_bind()
    existing = connection.execute(
        sa.text('SELECT id, country_code, top, "left", is_capital FROM cities')
    ).mappings().all()
    for city in existing:
        catalog = catalogs["ru"].get(city["id"])
        if catalog is None:
            raise ValueError(f"Existing city ID {city['id']} is missing from the catalogs.")
        if any(city[key] != catalog[key] for key in _CITY_METADATA):
            raise ValueError(f"Existing city metadata differs from the catalogs for ID {city['id']}.")

    op.create_table("physical_cities", sa.Column("id", sa.Integer(), primary_key=True))
    connection.execute(
        sa.text("INSERT INTO physical_cities (id) VALUES (:id)"),
        [{"id": city_id} for city_id in catalogs["ru"]],
    )
    op.drop_constraint("fk_routes_source_city_id_cities", "routes", type_="foreignkey")
    op.drop_constraint("fk_routes_target_city_id_cities", "routes", type_="foreignkey")
    op.drop_constraint("cities_pkey", "cities", type_="primary")
    op.execute("ALTER TABLE cities ALTER COLUMN id DROP IDENTITY IF EXISTS")
    op.add_column("cities", sa.Column("lang", sa.String(2), nullable=True))
    op.execute("UPDATE cities SET lang = 'ru'")
    op.alter_column("cities", "lang", nullable=False)
    op.create_primary_key("cities_pkey", "cities", ["id", "lang"])
    op.create_check_constraint("ck_cities_lang", "cities", "lang IN ('ru', 'en')")
    op.drop_index("uq_cities_name_lower", table_name="cities")
    op.create_index(
        "uq_cities_lang_name_lower", "cities", ["lang", sa.text("lower(name)")], unique=True
    )
    op.create_foreign_key(
        "fk_cities_physical_city_id", "cities", "physical_cities", ["id"], ["id"],
        ondelete="RESTRICT",
    )
    for side in ("source", "target"):
        op.create_foreign_key(
            f"fk_routes_{side}_city_id_physical_cities", "routes", "physical_cities",
            [f"{side}_city_id"], ["id"], ondelete="RESTRICT",
        )

    existing_ids = {row["id"] for row in existing}
    city_rows = [
        row for lang, catalog in catalogs.items()
        for city_id, row in catalog.items()
        if lang == "en" or city_id not in existing_ids
    ]
    connection.execute(
        sa.text(
            'INSERT INTO cities (id, lang, name, country_code, top, "left", is_capital) '
            'VALUES (:id, :lang, :name, :country_code, :top, :left, :is_capital)'
        ),
        city_rows,
    )
    for route in routes:
        existing_length = connection.execute(
            sa.text(
                "SELECT length_in_letters FROM routes "
                "WHERE (source_city_id = :source AND target_city_id = :target) "
                "OR (source_city_id = :target AND target_city_id = :source)"
            ),
            route,
        ).scalar_one_or_none()
        if existing_length is None:
            connection.execute(
                sa.text(
                    "INSERT INTO routes (source_city_id, target_city_id, length_in_letters) "
                    "VALUES (:source, :target, :length)"
                ),
                route,
            )
        elif existing_length != route["length"]:
            raise ValueError(f"Existing route differs from calculated length: {route!r}")


def downgrade() -> None:
    raise RuntimeError(
        "This migration cannot be downgraded without losing city translations. "
        "Restore a verified pre-migration backup instead."
    )

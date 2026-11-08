"""Refresh city catalog and seed the railway network.

Revision ID: c13d7a2f9e41
Revises: 7b8ea8dc3b0c
Create Date: 2026-10-27

"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c13d7a2f9e41"
down_revision: Union[str, None] = "7b8ea8dc3b0c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEGACY_COUNTRY_BY_CITY = {
    "Лондон": "GB",
    "Дублин": "IE",
    "Париж": "FR",
    "Лиссабон": "PT",
    "Мадрид": "ES",
    "Берн": "CH",
    "Рим": "IT",
    "Тирана": "AL",
    "София": "BG",
    "Афины": "GR",
    "Анкара": "TR",
    "Сочи": "RU",
    "Баку": "AZ",
    "Санкт-Петербург": "RU",
    "Москва": "RU",
    "Минск": "BY",
    "Киев": "UA",
    "Бухарест": "RO",
    "Белград": "RS",
    "Загреб": "HR",
    "Будапешт": "HU",
    "Вена": "AT",
    "Прага": "CZ",
    "Варшава": "PL",
    "Берлин": "DE",
    "Люксембург": "LU",
    "Амстердам": "NL",
    "Копенгаген": "DK",
    "Стокгольм": "SE",
    "Осло": "NO",
    "Хельсинки": "FI",
    "Таллин": "EE",
    "Рига": "LV",
    "Вильнюс": "LT",
}
_LEGACY_CAPITALS = set(_LEGACY_COUNTRY_BY_CITY) - {"Сочи"}
_ISO_ALPHA2 = re.compile(r"^[A-Z]{2}$")


def _read_catalogs() -> tuple[list[dict[str, object]], list[dict[str, int]]]:
    migrations_dir = Path(__file__).resolve().parents[1]
    with (migrations_dir / "cities.json").open("r", encoding="utf-8") as source:
        city_payload = json.load(source)
    with (migrations_dir / "routes.json").open("r", encoding="utf-8") as source:
        route_payload = json.load(source)

    raw_cities = city_payload.get("cities")
    raw_routes = route_payload.get("routes")
    if not isinstance(raw_cities, list) or not isinstance(raw_routes, list):
        raise ValueError("City and route seed files must contain arrays.")

    cities: list[dict[str, object]] = []
    city_ids: set[int] = set()
    city_names: set[str] = set()
    for row in raw_cities:
        if not isinstance(row, dict):
            raise ValueError("Every city seed record must be an object.")
        city_id = row.get("id")
        name = row.get("name")
        country_code = row.get("country_code")
        top = row.get("top")
        left = row.get("left")
        is_capital = row.get("is_capital")
        if (
            type(city_id) is not int
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(country_code, str)
            or not _ISO_ALPHA2.fullmatch(country_code)
            or isinstance(top, bool)
            or not isinstance(top, (int, float))
            or not math.isfinite(top)
            or not 0 <= top <= 3000
            or isinstance(left, bool)
            or not isinstance(left, (int, float))
            or not math.isfinite(left)
            or not 0 <= left <= 4000
            or type(is_capital) is not bool
        ):
            raise ValueError(f"Invalid city seed record: {row!r}")
        normalized_name = name.strip().casefold()
        if city_id <= 0 or city_id in city_ids or normalized_name in city_names:
            raise ValueError(f"Duplicate or invalid city ID/name: {row!r}")
        city_ids.add(city_id)
        city_names.add(normalized_name)
        cities.append(
            {
                "id": city_id,
                "name": name.strip(),
                "country_code": country_code,
                "top": float(top),
                "left": float(left),
                "is_capital": is_capital,
            }
        )

    routes: list[dict[str, int]] = []
    route_pairs: set[tuple[int, int]] = set()
    for row in raw_routes:
        if not isinstance(row, dict):
            raise ValueError("Every route seed record must be an object.")
        source_id = row.get("source_city_id")
        target_id = row.get("target_city_id")
        length = row.get("length_in_letters")
        if (
            type(source_id) is not int
            or type(target_id) is not int
            or type(length) is not int
            or source_id not in city_ids
            or target_id not in city_ids
            or source_id == target_id
            or length <= 0
            or length > 32767
        ):
            raise ValueError(f"Invalid route seed record: {row!r}")
        pair = tuple(sorted((source_id, target_id)))
        if pair in route_pairs:
            raise ValueError(f"Duplicate route between cities {pair}.")
        route_pairs.add(pair)
        routes.append(
            {
                "source_city_id": source_id,
                "target_city_id": target_id,
                "length_in_letters": length,
            }
        )

    if not cities or not routes:
        raise ValueError("City and route seed catalogs must not be empty.")

    adjacency = {city_id: set() for city_id in city_ids}
    for route in routes:
        source_id = route["source_city_id"]
        target_id = route["target_city_id"]
        adjacency[source_id].add(target_id)
        adjacency[target_id].add(source_id)

    visited: set[int] = set()
    pending = [next(iter(city_ids))]
    while pending:
        city_id = pending.pop()
        if city_id not in visited:
            visited.add(city_id)
            pending.extend(adjacency[city_id] - visited)
    if visited != city_ids:
        raise ValueError(f"Route graph is disconnected; unreachable city IDs: {city_ids - visited}")

    return cities, routes


def _insert_cities(cities: list[dict[str, object]]) -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            INSERT INTO cities (id, name, country_code, top, "left", is_capital)
            VALUES (:id, :name, :country_code, :top, :left, :is_capital)
            """
        ),
        cities,
    )
    connection.execute(
        sa.text(
            """
            SELECT setval(
                pg_get_serial_sequence('cities', 'id'),
                COALESCE((SELECT MAX(id) FROM cities), 1),
                EXISTS(SELECT 1 FROM cities)
            )
            """
        )
    )


def _insert_routes(routes: list[dict[str, int]]) -> None:
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO routes (source_city_id, target_city_id, length_in_letters)
            VALUES (:source_city_id, :target_city_id, :length_in_letters)
            """
        ),
        routes,
    )


def upgrade() -> None:
    cities, routes = _read_catalogs()
    connection = op.get_bind()

    connection.execute(sa.text("DELETE FROM routes"))
    connection.execute(sa.text("DELETE FROM cities"))
    _insert_cities(cities)
    _insert_routes(routes)


def downgrade() -> None:
    current_cities, _ = _read_catalogs()
    legacy_cities = [
        {
            **city,
            "country_code": _LEGACY_COUNTRY_BY_CITY[str(city["name"])],
            "is_capital": str(city["name"]) in _LEGACY_CAPITALS,
        }
        for city in current_cities
        if int(city["id"]) <= 34
    ]

    connection = op.get_bind()
    connection.execute(sa.text("DELETE FROM routes"))
    connection.execute(sa.text("DELETE FROM cities"))
    _insert_cities(legacy_cities)

"""Seed city catalog from JSON into the cities table.

Revision ID: 7b8ea8dc3b0c
Revises: a84c91d2e7b3
Create Date: 2026-10-25

"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "7b8ea8dc3b0c"
down_revision: Union[str, None] = "a84c91d2e7b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COUNTRY_BY_CITY = {
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

_CAPITALS = {
    "Лондон",
    "Дублин",
    "Париж",
    "Лиссабон",
    "Мадрид",
    "Берн",
    "Рим",
    "Тирана",
    "София",
    "Афины",
    "Анкара",
    "Баку",
    "Санкт-Петербург",
    "Москва",
    "Минск",
    "Киев",
    "Бухарест",
    "Белград",
    "Загреб",
    "Будапешт",
    "Вена",
    "Прага",
    "Варшава",
    "Берлин",
    "Люксембург",
    "Амстердам",
    "Копенгаген",
    "Стокгольм",
    "Осло",
    "Хельсинки",
    "Таллин",
    "Рига",
    "Вильнюс",
}


def _load_city_seed_rows() -> list[dict[str, object]]:
    json_path = Path(__file__).resolve().parents[1] / "cities.json"
    with json_path.open("r", encoding="utf-8") as source:
        payload = json.load(source)

    rows: list[dict[str, object]] = []
    for city in payload.get("cities", []):
        name = str(city["name"]).strip()
        rows.append(
            {
                "id": int(city["id"]),
                "name": name,
                "country_code": _COUNTRY_BY_CITY.get(name, "RU"),
                "top": float(city["top"]),
                "left": float(city["left"]),
                "is_capital": name in _CAPITALS,
            }
        )
    return rows


def upgrade() -> None:
    connection = op.get_bind()
    existing_ids: set[int] = set(
        connection.execute(sa.text("SELECT id FROM cities")).scalars().all()
    )
    existing_names: set[str] = {
        name for name in connection.execute(sa.text("SELECT lower(name) FROM cities")).scalars().all()
    }

    for city in _load_city_seed_rows():
        city_id = int(city["id"])
        city_name = str(city["name"])
        normalized_name = city_name.casefold()
        if city_id in existing_ids or normalized_name in existing_names:
            continue

        connection.execute(
            sa.text(
                """
                INSERT INTO cities (id, name, country_code, top, "left", is_capital)
                VALUES (:id, :name, :country_code, :top, :left, :is_capital)
                """
            ),
            {
                "id": city_id,
                "name": city_name,
                "country_code": str(city["country_code"]),
                "top": float(city["top"]),
                "left": float(city["left"]),
                "is_capital": bool(city["is_capital"]),
            },
        )

        existing_ids.add(city_id)
        existing_names.add(normalized_name)

    connection.execute(
        sa.text(
            "SELECT setval(pg_get_serial_sequence('cities', 'id'), COALESCE((SELECT MAX(id) FROM cities), 0), true)"
        )
    )


def downgrade() -> None:
    seed_ids = {int(row["id"]) for row in _load_city_seed_rows()}
    connection = op.get_bind()
    if not seed_ids:
        return
    for city_id in sorted(seed_ids):
        connection.execute(
            sa.text("DELETE FROM cities WHERE id = :city_id"),
            {"city_id": city_id},
        )

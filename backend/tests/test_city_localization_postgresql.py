import os
import unittest

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal, engine
from app.models import City


@unittest.skipUnless(
    os.getenv("RAILWAY_RUN_POSTGRES_INTEGRATION") == "1",
    "Set RAILWAY_RUN_POSTGRES_INTEGRATION=1 for a disposable PostgreSQL database.",
)
class CityLocalizationPostgreSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await engine.dispose()

    async def test_language_queries_keep_one_translation_per_physical_city(self) -> None:
        async with AsyncSessionLocal() as session:
            ru = (await session.scalars(select(City).where(City.lang == "ru"))).all()
            en = (await session.scalars(select(City).where(City.lang == "en"))).all()
            self.assertEqual(len(ru), len({city.id for city in ru}))
            self.assertEqual({city.id for city in ru}, {city.id for city in en})
            london = next(city for city in en if city.id == 1)
            self.assertEqual(london.name, "London")
            self.assertEqual(next(city for city in ru if city.id == 1).name, "Лондон")

    async def test_routes_reject_unknown_physical_city(self) -> None:
        async with AsyncSessionLocal() as session:
            with self.assertRaises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO routes (source_city_id, target_city_id, length_in_letters) "
                        "VALUES (1, 2147483647, 5)"
                    )
                )
            await session.rollback()

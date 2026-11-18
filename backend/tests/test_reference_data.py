import json
import math
import unittest
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


class ReferenceDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with (MIGRATIONS_DIR / "cities.json").open(encoding="utf-8") as source:
            cls.cities = json.load(source)["cities"]
        with (MIGRATIONS_DIR / "routes.json").open(encoding="utf-8") as source:
            cls.routes = json.load(source)["routes"]

    def test_city_seed_matches_city_model_requirements(self) -> None:
        ids = [city["id"] for city in self.cities]
        normalized_names = [city["name"].casefold() for city in self.cities]

        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(normalized_names), len(set(normalized_names)))
        for city in self.cities:
            with self.subTest(city=city["name"]):
                self.assertIsInstance(city["id"], int)
                self.assertTrue(city["name"].strip())
                self.assertRegex(city["country_code"], r"^[A-Z]{2}$")
                self.assertIsInstance(city["top"], (int, float))
                self.assertGreaterEqual(city["top"], 0)
                self.assertLessEqual(city["top"], 3000)
                self.assertIsInstance(city["left"], (int, float))
                self.assertGreaterEqual(city["left"], 0)
                self.assertLessEqual(city["left"], 4000)
                self.assertIs(type(city["is_capital"]), bool)

    def test_route_seed_references_unique_valid_city_pairs(self) -> None:
        city_ids = {city["id"] for city in self.cities}
        route_pairs = [
            tuple(sorted((route["source_city_id"], route["target_city_id"])))
            for route in self.routes
        ]

        self.assertEqual(len(route_pairs), len(set(route_pairs)))
        for route in self.routes:
            with self.subTest(route=route):
                self.assertIn(route["source_city_id"], city_ids)
                self.assertIn(route["target_city_id"], city_ids)
                self.assertNotEqual(route["source_city_id"], route["target_city_id"])
                self.assertGreater(route["length_in_letters"], 0)

    def test_route_graph_connects_every_city(self) -> None:
        city_ids = {city["id"] for city in self.cities}
        adjacency = {city_id: set() for city_id in city_ids}
        for route in self.routes:
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

        self.assertEqual(visited, city_ids)

    def test_localized_catalogs_share_ids_and_physical_coordinates(self) -> None:
        localized = {}
        for language in ("ru", "en"):
            with (MIGRATIONS_DIR / f"cities_{language}.json").open(encoding="utf-8") as source:
                cities = json.load(source)["cities"]
            ids = [city["id"] for city in cities]
            self.assertEqual(len(ids), len(set(ids)))
            localized[language] = {city["id"]: city for city in cities}

        self.assertEqual(set(localized["ru"]), set(localized["en"]))
        for city_id, city in localized["ru"].items():
            english = localized["en"][city_id]
            for key in ("country_code", "top", "left", "is_capital"):
                self.assertEqual(city[key], english[key], (city_id, key))

    def test_new_route_lengths_match_map_geometry(self) -> None:
        cities = {city["id"]: city for city in self.cities}
        expected_pairs = {(15, 34), (12, 37), (15, 37)}
        found = set()
        for route in self.routes:
            pair = (route["source_city_id"], route["target_city_id"])
            if pair in expected_pairs:
                source, target = (cities[city_id] for city_id in pair)
                expected = math.floor(
                    math.hypot(source["left"] - target["left"], source["top"] - target["top"])
                    / 26
                )
                self.assertEqual(route["length_in_letters"], expected)
                found.add(pair)
        self.assertEqual(found, expected_pairs)


if __name__ == "__main__":
    unittest.main()

import random
import unittest
from types import SimpleNamespace

from app.route_paths import (
    eligible_city_pairs,
    find_random_route_path,
    is_continuous_path,
)


def route(route_id: int, source: int, target: int, length: int = 10):
    return SimpleNamespace(
        id=route_id, source_city_id=source, target_city_id=target, length_in_letters=length
    )


# 1 - 2 - 3 and a longer way 1 - 4 - 3; city 5 hangs off 3; 6 - 7 is a separate island.
ROUTES = [
    route(1, 1, 2), route(2, 2, 3), route(3, 1, 4), route(4, 4, 3),
    route(5, 3, 5), route(6, 6, 7),
]
BY_ID = {item.id: item for item in ROUTES}


class ContinuousPathTests(unittest.TestCase):
    def test_accepts_ordered_paths_in_either_direction(self) -> None:
        self.assertTrue(is_continuous_path(BY_ID, [1, 2], 1, 3))
        self.assertTrue(is_continuous_path(BY_ID, [2, 1], 3, 1))
        self.assertTrue(is_continuous_path(BY_ID, [3, 4, 5], 1, 5))

    def test_rejects_gaps_wrong_ends_and_wrong_order(self) -> None:
        self.assertFalse(is_continuous_path(BY_ID, [2, 1], 1, 3))
        self.assertFalse(is_continuous_path(BY_ID, [1], 1, 3))
        self.assertFalse(is_continuous_path(BY_ID, [1, 5], 1, 5))
        self.assertFalse(is_continuous_path(BY_ID, [1, 2], 2, 3))

    def test_rejects_cycles_and_repeated_segments(self) -> None:
        self.assertFalse(is_continuous_path(BY_ID, [1, 2, 4, 3], 1, 1))
        self.assertFalse(is_continuous_path(BY_ID, [1, 1], 1, 1))
        self.assertFalse(is_continuous_path(BY_ID, [1, 2, 5, 5], 1, 3))


class RandomPathTests(unittest.TestCase):
    def test_finds_both_alternatives_over_many_seeds(self) -> None:
        found = {
            tuple(find_random_route_path(ROUTES, 1, 3, rng=random.Random(seed)))
            for seed in range(30)
        }
        self.assertEqual(found, {(1, 2), (3, 4)})

    def test_result_is_a_valid_path(self) -> None:
        for seed in range(20):
            path = find_random_route_path(ROUTES, 1, 5, rng=random.Random(seed))
            self.assertTrue(is_continuous_path(BY_ID, path, 1, 5))

    def test_unreachable_cities_and_short_segments_have_no_path(self) -> None:
        self.assertIsNone(find_random_route_path(ROUTES, 1, 7))
        short = [route(1, 1, 2, length=2)]
        self.assertIsNone(find_random_route_path(short, 1, 2))

    def test_acceptance_filter_backtracks_to_another_path(self) -> None:
        for seed in range(20):
            path = find_random_route_path(
                ROUTES, 1, 3, rng=random.Random(seed), accept=lambda p: p != [1, 2]
            )
            self.assertEqual(path, [3, 4])

    def test_no_acceptable_path_and_search_budget(self) -> None:
        self.assertIsNone(find_random_route_path(ROUTES, 1, 3, accept=lambda path: False))
        self.assertIsNone(find_random_route_path(ROUTES, 1, 3, max_steps=1))


class EligiblePairTests(unittest.TestCase):
    def test_pairs_stay_inside_connected_components(self) -> None:
        pairs = eligible_city_pairs(range(1, 8), ROUTES, rng=random.Random(1))
        unordered = {frozenset(pair) for pair in pairs}
        self.assertEqual(len(pairs), len(unordered))
        self.assertEqual(len(unordered), 10 + 1)  # C(5, 2) in the large island, one pair in 6-7
        self.assertNotIn(frozenset((1, 6)), unordered)
        self.assertIn(frozenset((6, 7)), unordered)

    def test_pairs_are_oriented_randomly(self) -> None:
        directions = {
            pair[0] < pair[1]
            for seed in range(20)
            for pair in eligible_city_pairs([1, 2], ROUTES, rng=random.Random(seed))
        }
        self.assertEqual(directions, {True, False})

    def test_unusable_routes_do_not_connect_cities(self) -> None:
        self.assertEqual(eligible_city_pairs([1, 2], [route(1, 1, 2, length=2)]), [])


if __name__ == "__main__":
    unittest.main()

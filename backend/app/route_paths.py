"""Route-graph helpers of the continuous-game API: pair eligibility and path search."""
from __future__ import annotations

import itertools
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Protocol

# A segment must be able to host at least one word of three letters.
MIN_USABLE_ROUTE_LETTERS = 3
DEFAULT_SEARCH_STEPS = 50_000


class RouteLike(Protocol):
    id: int
    source_city_id: int
    target_city_id: int
    length_in_letters: int


def is_continuous_path(
    routes_by_id: Mapping[int, RouteLike],
    route_ids: Sequence[int],
    source_city_id: int,
    target_city_id: int,
) -> bool:
    """Check order, continuity, the two assigned ends and the absence of repeated cities."""
    city_id = source_city_id
    visited = {source_city_id}
    for route_id in route_ids:
        route = routes_by_id[route_id]
        if city_id == route.source_city_id:
            next_city_id = route.target_city_id
        elif city_id == route.target_city_id:
            next_city_id = route.source_city_id
        else:
            return False
        if next_city_id in visited:
            return False
        visited.add(next_city_id)
        city_id = next_city_id
    return city_id == target_city_id


def _adjacency(
    routes: Iterable[RouteLike], rng: random.Random
) -> dict[int, list[tuple[int, int]]]:
    adjacency: dict[int, list[tuple[int, int]]] = {}
    for route in routes:
        if route.length_in_letters < MIN_USABLE_ROUTE_LETTERS:
            continue
        adjacency.setdefault(route.source_city_id, []).append(
            (route.id, route.target_city_id)
        )
        adjacency.setdefault(route.target_city_id, []).append(
            (route.id, route.source_city_id)
        )
    for edges in adjacency.values():
        rng.shuffle(edges)
    return adjacency


def find_random_route_path(
    routes: Iterable[RouteLike],
    source_city_id: int,
    target_city_id: int,
    *,
    accept: Callable[[list[int]], bool] | None = None,
    rng: random.Random | None = None,
    max_steps: int = DEFAULT_SEARCH_STEPS,
) -> list[int] | None:
    """Find a random simple path of usable segments that ``accept`` allows.

    The search backtracks, so a rejected path does not hide other paths; the step
    budget keeps a pathological catalog from blocking the request.
    """
    rng = rng or random.SystemRandom()
    adjacency = _adjacency(routes, rng)
    visited = {source_city_id}
    path: list[int] = []
    stack = [(source_city_id, iter(adjacency.get(source_city_id, [])))]
    steps = 0
    while stack:
        city_id, edges = stack[-1]
        try:
            route_id, next_city_id = next(edges)
        except StopIteration:
            stack.pop()
            visited.discard(city_id)
            if path:
                path.pop()
            continue
        if next_city_id in visited:
            continue
        steps += 1
        if steps > max_steps:
            return None
        path.append(route_id)
        if next_city_id == target_city_id:
            if accept is None or accept(path):
                return list(path)
            path.pop()
            continue
        visited.add(next_city_id)
        stack.append((next_city_id, iter(adjacency.get(next_city_id, []))))
    return None


def eligible_city_pairs(
    city_ids: Sequence[int],
    routes: Iterable[RouteLike],
    *,
    rng: random.Random | None = None,
) -> list[tuple[int, int]]:
    """Unordered city pairs joined by usable routes, each with a random direction."""
    rng = rng or random.SystemRandom()
    parent = {city_id: city_id for city_id in city_ids}

    def find(city_id: int) -> int:
        while parent[city_id] != city_id:
            parent[city_id] = parent[parent[city_id]]
            city_id = parent[city_id]
        return city_id

    for route in routes:
        if (
            route.length_in_letters >= MIN_USABLE_ROUTE_LETTERS
            and route.source_city_id in parent
            and route.target_city_id in parent
        ):
            parent[find(route.source_city_id)] = find(route.target_city_id)
    pairs = []
    for first, second in itertools.combinations(city_ids, 2):
        if find(first) == find(second):
            pairs.append((first, second) if rng.getrandbits(1) else (second, first))
    return pairs

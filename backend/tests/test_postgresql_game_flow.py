import heapq
import os
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import DictionaryWord, GameResult, User
from main import app, check_game_duration, lifespan, settings


@unittest.skipUnless(
    os.getenv("RAILWAY_RUN_POSTGRES_INTEGRATION") == "1",
    "Set RAILWAY_RUN_POSTGRES_INTEGRATION=1 to run against a disposable PostgreSQL database.",
)
class PostgreSQLGameFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = get_settings()

    async def _create_account(
        self,
        client: AsyncClient,
        suffix: str,
    ) -> tuple[str, str, str]:
        username = f"task27-{suffix}"
        email = f"{username}@example.com"
        password = "task27-integration-password"
        registered = await client.post(
            "/auth/register/",
            json={"username": username, "email": email, "password": password},
        )
        self.assertEqual(registered.status_code, 201, registered.text)

        logged_in = await client.post(
            "/auth/",
            json={"email": email, "password": password},
        )
        self.assertEqual(logged_in.status_code, 200, logged_in.text)
        access_token = client.cookies.get(self.settings.auth_cookie_name)
        csrf_token = client.cookies.get(self.settings.csrf_cookie_name)
        self.assertIsNotNone(access_token)
        self.assertIsNotNone(csrf_token)
        return username, access_token, csrf_token

    def _use_account(self, client: AsyncClient, account: tuple[str, str, str]) -> None:
        _, access_token, csrf_token = account
        client.cookies.clear()
        client.cookies.set(self.settings.auth_cookie_name, access_token)
        client.cookies.set(self.settings.csrf_cookie_name, csrf_token)

    @staticmethod
    def _route_path(
        routes: list[dict[str, int]],
        source: int,
        target: int,
    ) -> list[int]:
        adjacency: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
        for route in routes:
            route_id = route["id"]
            length = route["length_in_letters"]
            adjacency[route["source_city_id"]].append((route["target_city_id"], route_id, length))
            adjacency[route["target_city_id"]].append((route["source_city_id"], route_id, length))

        queue: list[tuple[int, int, int, tuple[int, ...], tuple[int, ...]]] = [
            (0, 0, source, (source,), ())
        ]
        best_depth: int | None = None
        candidates: list[tuple[int, tuple[int, ...]]] = []
        while queue:
            depth, negative_capacity, city_id, city_path, route_path = heapq.heappop(queue)
            if best_depth is not None and depth > best_depth:
                break
            if city_id == target:
                best_depth = depth
                candidates.append((-negative_capacity, route_path))
                continue
            for next_city, route_id, length in adjacency[city_id]:
                if next_city in city_path:
                    continue
                capacity = max(0, length - 2)
                heapq.heappush(
                    queue,
                    (
                        depth + 1,
                        negative_capacity - capacity,
                        next_city,
                        city_path + (next_city,),
                        route_path + (route_id,),
                    ),
                )

        if not candidates:
            raise AssertionError(f"No route path connects cities {source} and {target}.")
        return list(max(candidates, key=lambda candidate: candidate[0])[1])

    async def _word_answers(self, word_ids: set[int]) -> dict[int, str]:
        async with AsyncSessionLocal() as session:
            rows = await session.scalars(
                select(DictionaryWord).where(DictionaryWord.id.in_(word_ids))
            )
            return {word.id: word.word_text for word in rows.all()}

    async def _fail_winner_transaction_after_score_write(
        self,
        session,
        game,
        finish,
    ) -> None:
        await session.rollback()
        async with session.begin():
            session.add(
                GameResult(
                    game_id=finish.game_id,
                    winner_user_id=finish.winner_user_id,
                    started_at=game.started_at,
                    finished_at=finish.finished_at,
                    finish_reason="winner",
                    score_awarded=finish.score_awarded,
                )
            )
            await session.flush()
            await session.execute(
                update(User)
                .where(User.id == finish.winner_user_id)
                .values(score=User.score + finish.score_awarded)
            )
            raise SQLAlchemyError("Injected failure after writing winner and score.")

    async def _persisted_result_and_score(
        self,
        round_id: str,
        username: str,
    ) -> tuple[int, int]:
        async with AsyncSessionLocal() as session:
            result_count = await session.scalar(
                select(func.count()).select_from(GameResult).where(
                    GameResult.game_id == UUID(round_id)
                )
            )
            user = await session.scalar(
                select(User).where(User.username == username)
            )
            return result_count, user.score if user else -1

    async def test_registration_login_gameplay_persistence_timeout_and_next_round(
        self,
    ) -> None:
        suffix = uuid4().hex[:12]

        async with lifespan(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                winner_account = await self._create_account(client, f"{suffix}-winner")
                other_account = await self._create_account(client, f"{suffix}-other")
                routes_response = await client.get("/game/routes/")
                self.assertEqual(routes_response.status_code, 200, routes_response.text)
                route_catalog = routes_response.json()["routes"]

                self._use_account(client, winner_account)
                winner_join = await client.post(
                    "/game/join/",
                    headers={"X-CSRF-Token": winner_account[2]},
                )
                self.assertEqual(winner_join.status_code, 200, winner_join.text)
                round_id = winner_join.json()["round"]["id"]
                winner = winner_join.json()["player"]
                winner_route_ids = self._route_path(
                    route_catalog,
                    winner["source_city_id"],
                    winner["target_city_id"],
                )
                winner_route = await client.post(
                    "/game/route/",
                    json={"round_id": round_id, "route_ids": winner_route_ids},
                    headers={"X-CSRF-Token": winner_account[2]},
                )
                self.assertEqual(winner_route.status_code, 200, winner_route.text)
                self.assertEqual(winner_route.json()["player"]["state"], "playing")
                self.assertTrue(winner_route.json()["crossword"])

                self._use_account(client, other_account)
                other_join = await client.post(
                    "/game/join/",
                    headers={"X-CSRF-Token": other_account[2]},
                )
                self.assertEqual(other_join.status_code, 200, other_join.text)
                other_player = other_join.json()["player"]
                other_route_ids = self._route_path(
                    route_catalog,
                    other_player["source_city_id"],
                    other_player["target_city_id"],
                )
                other_route = await client.post(
                    "/game/route/",
                    json={"round_id": round_id, "route_ids": other_route_ids},
                    headers={"X-CSRF-Token": other_account[2]},
                )
                self.assertEqual(other_route.status_code, 200, other_route.text)

                self._use_account(client, winner_account)
                placements = winner_route.json()["crossword"]
                game = app.state.activeGame
                shared_route_ids = set(winner_route_ids).intersection(other_route_ids)
                if shared_route_ids:
                    winner_shared_words = {
                        placement["placement_id"]
                        for placement in placements
                        if placement["route_id"] in shared_route_ids
                    }
                    other_shared_words = {
                        placement["placement_id"]
                        for placement in other_route.json()["crossword"]
                        if placement["route_id"] in shared_route_ids
                    }
                    self.assertEqual(winner_shared_words, other_shared_words)

                placement_by_id = {
                    str(placement.placement_id): placement
                    for route_id in winner_route_ids
                    for placement in game.shared_words[route_id]
                }
                word_ids = {
                    placement.dictionary_word_id
                    for placement in placement_by_id.values()
                }
                answers = await self._word_answers(word_ids)
                self.assertEqual(set(answers), word_ids)

                wrong_answer = await client.post(
                    "/game/guess/",
                    json={
                        "round_id": round_id,
                        "placement_id": placements[0]["placement_id"],
                        "answer": "wrong-task27-answer",
                    },
                    headers={"X-CSRF-Token": winner_account[2]},
                )
                self.assertEqual(wrong_answer.status_code, 200, wrong_answer.text)
                self.assertEqual(wrong_answer.json()["guess"]["result"], "incorrect")
                self.assertNotIn("revealed_word", wrong_answer.json()["guess"])

                final_placement = placements[-1]
                final_state_placement = placement_by_id[final_placement["placement_id"]]
                awarded_points = 0
                for crossword_placement in placements[:-1]:
                    state_placement = placement_by_id[crossword_placement["placement_id"]]
                    guess = await client.post(
                        "/game/guess/",
                        json={
                            "round_id": round_id,
                            "placement_id": crossword_placement["placement_id"],
                            "answer": answers[state_placement.dictionary_word_id],
                        },
                        headers={"X-CSRF-Token": winner_account[2]},
                    )
                    self.assertEqual(guess.status_code, 200, guess.text)
                    self.assertEqual(guess.json()["guess"]["result"], "correct")
                    awarded_points += guess.json()["guess"]["points_awarded"]

                with patch(
                    "app.game_api._persist_winner",
                    new=self._fail_winner_transaction_after_score_write,
                ):
                    failed_finish = await client.post(
                        "/game/guess/",
                        json={
                            "round_id": round_id,
                            "placement_id": final_placement["placement_id"],
                            "answer": answers[final_state_placement.dictionary_word_id],
                        },
                        headers={"X-CSRF-Token": winner_account[2]},
                    )
                self.assertEqual(failed_finish.status_code, 503, failed_finish.text)
                self.assertEqual(
                    failed_finish.json()["error"]["code"],
                    "winner_persistence_failed",
                )
                self.assertEqual(app.state.activeGame.status, "active")
                rolled_back_results, rolled_back_score = (
                    await self._persisted_result_and_score(round_id, winner_account[0])
                )
                self.assertEqual(rolled_back_results, 0)
                self.assertEqual(rolled_back_score, 0)

                successful_finish = await client.post(
                    "/game/guess/",
                    json={
                        "round_id": round_id,
                        "placement_id": final_placement["placement_id"],
                        "answer": answers[final_state_placement.dictionary_word_id],
                    },
                    headers={"X-CSRF-Token": winner_account[2]},
                )
                self.assertEqual(successful_finish.status_code, 200, successful_finish.text)
                self.assertEqual(successful_finish.json()["player"]["state"], "won")
                awarded_points += successful_finish.json()["guess"]["points_awarded"]
                self.assertEqual(app.state.prevGame.round_id.hex, round_id.replace("-", ""))

                duplicate_finish = await client.post(
                    "/game/guess/",
                    json={
                        "round_id": round_id,
                        "placement_id": final_placement["placement_id"],
                        "answer": answers[final_state_placement.dictionary_word_id],
                    },
                    headers={"X-CSRF-Token": winner_account[2]},
                )
                self.assertEqual(duplicate_finish.status_code, 200, duplicate_finish.text)
                self.assertEqual(duplicate_finish.json()["player"]["state"], "won")
                self.assertIsNone(duplicate_finish.json().get("guess"))

                self._use_account(client, other_account)
                loser_sync = await client.get(f"/game/sync/?round_id={round_id}")
                self.assertEqual(loser_sync.status_code, 200, loser_sync.text)
                self.assertEqual(loser_sync.json()["player"]["state"], "lost")

                leaderboard = await client.get("/game/stats/")
                self.assertEqual(leaderboard.status_code, 200, leaderboard.text)
                winner_score = next(
                    entry["score"]
                    for entry in leaderboard.json()["leaderboard"]
                    if entry["username"] == winner_account[0]
                )
                self.assertEqual(winner_score, awarded_points)

                result_count, persisted_score = await self._persisted_result_and_score(
                    round_id,
                    winner_account[0],
                )
                self.assertEqual(result_count, 1)
                self.assertEqual(persisted_score, awarded_points)

                timed_out_round = app.state.activeGame
                timed_out_round.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await check_game_duration(app, app_settings=settings)
                self.assertEqual(app.state.prevGame.round_id, timed_out_round.game_id)
                self.assertEqual(app.state.prevGame.finish_reason, "timeout")
                self.assertNotEqual(app.state.activeGame.game_id, timed_out_round.game_id)

                timeout_sync = await client.get(
                    f"/game/sync/?round_id={timed_out_round.game_id}"
                )
                self.assertEqual(timeout_sync.status_code, 200, timeout_sync.text)
                self.assertEqual(timeout_sync.json()["round"]["finish_reason"], "timeout")

                self._use_account(client, winner_account)
                next_round_join = await client.post(
                    "/game/join/",
                    headers={"X-CSRF-Token": winner_account[2]},
                )
                self.assertEqual(next_round_join.status_code, 200, next_round_join.text)
                self.assertEqual(
                    next_round_join.json()["round"]["id"],
                    str(app.state.activeGame.game_id),
                )
                self.assertEqual(
                    next_round_join.json()["player"]["state"],
                    "selecting_route",
                )


if __name__ == "__main__":
    unittest.main()

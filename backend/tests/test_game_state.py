import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from pydantic import SecretStr

from app.config import Settings
from app.game_state import (
    ActiveGame,
    GameFinishedError,
    GameExpiredError,
    GuessResult,
    HintUsed,
    InsufficientScoreError,
    RoundNotExpiredError,
    RouteNotAvailableError,
    WordPlacement,
    _normalize_guess,
)


class ActiveGameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.started_at = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        self.settings = Settings(
            jwt_secret=SecretStr("test-secret-for-unit-tests-only-32-bytes-minimum"),
            game_duration_hours=1,
            hint_first_letter_cost=5,
            hint_last_letter_cost=7,
        )

    def create_game(self) -> ActiveGame:
        return ActiveGame.create(settings=self.settings, started_at=self.started_at)

    def test_guess_normalization_applies_only_contract_transformations(self) -> None:
        """Normalize whitespace, lowercase, and yo without other substitutions."""
        self.assertEqual(_normalize_guess("  ЁЖ! "), "еж!")
        self.assertEqual(_normalize_guess("Straße"), "straße")

    async def create_shared_route_game(self) -> tuple[ActiveGame, UUID, UUID]:
        game = self.create_game()
        first_player_id, second_player_id = uuid4(), uuid4()
        await game.add_player(first_player_id, 1, 3, starting_score=20, now=self.started_at)
        await game.add_player(second_player_id, 4, 3, starting_score=20, now=self.started_at)

        route_words = {
            101: (1, 1, 3),
            102: (3, 1, 5),
            103: (2, 1, 4),
        }
        for route_id, (word_id, position, letter_count) in route_words.items():
            await game.add_shared_words(
                route_id,
                [
                    WordPlacement(
                        dictionary_word_id=word_id,
                        route_id=route_id,
                        position_index=position,
                        letter_count=letter_count,
                    )
                ],
                now=self.started_at,
            )

        endpoints = {101: (1, 2), 102: (2, 3), 103: (4, 2)}
        await game.set_player_route(
            first_player_id, [101, 102], endpoints, now=self.started_at
        )
        await game.set_player_route(
            second_player_id, [103, 102], endpoints, now=self.started_at
        )
        return game, first_player_id, second_player_id

    async def test_concurrent_shared_guess_awards_points_once_and_finishes_once(self) -> None:
        """Serialize concurrent guesses so a shared answer is awarded once."""
        game, first_player_id, second_player_id = await self.create_shared_route_game()
        self.assertEqual(game.expires_at - game.started_at, timedelta(hours=1))
        self.assertEqual(game.hint_first_letter_cost, 5)
        self.assertEqual(game.hint_last_letter_cost, 7)

        await game.guess_word(
            first_player_id, 101, 1, " alpha ", "Alpha", now=self.started_at
        )
        await game.guess_word(
            second_player_id, 103, 2, "beta", "Beta", now=self.started_at
        )

        results = await asyncio.gather(
            game.guess_word(
                first_player_id, 102, 3, "gamma", "Gamma", now=self.started_at
            ),
            game.guess_word(
                second_player_id, 102, 3, "gamma", "Gamma", now=self.started_at
            ),
            return_exceptions=True,
        )

        successful_guesses = [result for result in results if isinstance(result, GuessResult)]
        self.assertEqual(len(successful_guesses), 1)
        self.assertEqual(successful_guesses[0].points_awarded, 50)
        self.assertEqual(sum(result.points_awarded for result in successful_guesses), 50)
        self.assertEqual(game.status, "finished")
        self.assertEqual(game.finish_reason, "winner")
        self.assertEqual(game.winner_user_id, successful_guesses[0].guessed_by_user_id)
        self.assertEqual(game.score_awarded, 100)
        self.assertTrue(game.shared_words[102][0].is_guessed)

        with self.assertRaises(GameFinishedError):
            await game.guess_word(
                first_player_id, 102, 3, "gamma", "Gamma", now=self.started_at
            )

    async def test_hint_is_charged_once_and_balance_is_tracked(self) -> None:
        game = self.create_game()
        user_id = uuid4()
        await game.add_player(user_id, 1, 2, starting_score=20, now=self.started_at)
        await game.add_shared_words(
            1,
            [
                WordPlacement(
                    dictionary_word_id=10,
                    route_id=1,
                    position_index=0,
                    letter_count=5,
                )
            ],
            now=self.started_at,
        )
        await game.set_player_route(user_id, [1], {1: (1, 2)}, now=self.started_at)

        first = await game.use_hint(
            user_id, 1, 10, "first_letter", "T", now=self.started_at
        )
        repeated = await game.use_hint(
            user_id, 1, 10, "first_letter", "T", now=self.started_at
        )
        last = await game.use_hint(
            user_id, 1, 10, "last_letter", "T", now=self.started_at
        )
        repeated_last = await game.use_hint(
            user_id, 1, 10, "last_letter", "T", now=self.started_at
        )

        self.assertEqual(first.points_charged, 5)
        self.assertEqual(first.score_balance, 15)
        self.assertTrue(repeated.already_used)
        self.assertEqual(repeated.points_charged, 0)
        self.assertEqual(repeated.score_balance, 15)
        self.assertEqual(last.points_charged, 7)
        self.assertEqual(last.score_balance, 8)
        self.assertTrue(repeated_last.already_used)
        self.assertEqual(repeated_last.points_charged, 0)
        self.assertEqual(repeated_last.score_balance, 8)

    async def test_hint_requires_sufficient_score(self) -> None:
        game = self.create_game()
        user_id = uuid4()
        await game.add_player(user_id, 1, 2, starting_score=4, now=self.started_at)
        await game.add_shared_words(
            1,
            [
                WordPlacement(
                    dictionary_word_id=10,
                    route_id=1,
                    position_index=0,
                    letter_count=5,
                )
            ],
            now=self.started_at,
        )
        await game.set_player_route(user_id, [1], {1: (1, 2)}, now=self.started_at)

        with self.assertRaises(InsufficientScoreError):
            await game.use_hint(
                user_id, 1, 10, "first_letter", "T", now=self.started_at
            )
        self.assertFalse(game.players[user_id].hints)
        self.assertEqual(game.players[user_id].hint_points_spent, 0)

    async def test_route_rejects_cycles_and_duplicate_words(self) -> None:
        game = self.create_game()
        user_id = uuid4()
        await game.add_player(user_id, 1, 4, now=self.started_at)
        for route_id, word_id in ((1, 10), (2, 10), (3, 11), (4, 12)):
            await game.add_shared_words(
                route_id,
                [
                    WordPlacement(
                        dictionary_word_id=word_id,
                        route_id=route_id,
                        position_index=0,
                        letter_count=4,
                    )
                ],
                now=self.started_at,
            )

        with self.assertRaises(ValueError):
            await game.set_player_route(
                user_id,
                [1, 2],
                {1: (1, 2), 2: (2, 4)},
                now=self.started_at,
            )

        with self.assertRaises(RouteNotAvailableError):
            await game.set_player_route(
                user_id,
                [1, 3, 4],
                {1: (1, 2), 3: (2, 3), 4: (3, 2)},
                now=self.started_at,
            )

    async def test_timeout_is_idempotent_and_winner_cannot_be_replaced(self) -> None:
        game = self.create_game()
        expiration = game.expires_at

        with self.assertRaises(RoundNotExpiredError):
            await game.finish_timeout(now=expiration - timedelta(minutes=1))

        first_result = await game.finish_timeout(now=expiration)
        repeated_result = await game.finish_timeout(now=expiration)

        self.assertEqual(first_result, repeated_result)
        self.assertEqual(game.status, "finished")
        self.assertEqual(game.finish_reason, "timeout")
        self.assertIsNone(game.winner_user_id)
        self.assertEqual(game.score_awarded, 0)

    async def test_operation_after_expiration_finishes_round_as_timeout(self) -> None:
        game = self.create_game()
        user_id = uuid4()

        with self.assertRaises(GameExpiredError):
            await game.add_player(user_id, 1, 2, now=game.expires_at)

        result = await game.finish_timeout(now=game.expires_at)
        self.assertEqual(result.finish_reason, "timeout")
        self.assertNotIn(user_id, game.players)

    async     def test_hint_flags_are_independent(self) -> None:
        hints = HintUsed()
        hints.mark_used("first_letter")

        self.assertTrue(hints.is_used("first_letter"))
        self.assertFalse(hints.is_used("last_letter"))


if __name__ == "__main__":
    unittest.main()

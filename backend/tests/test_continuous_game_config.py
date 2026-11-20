import os
import subprocess
import sys
import unittest
from pathlib import Path

from pydantic import SecretStr

from app.config import Settings

BACKEND = Path(__file__).resolve().parents[1]


class ContinuousGameSettingsTests(unittest.TestCase):
    def settings(self, **values) -> Settings:
        return Settings(
            jwt_secret=SecretStr("test-secret-for-continuous-game-settings"),
            language="en",
            **values,
        )

    def test_flag_is_off_by_default_and_needs_redis(self) -> None:
        self.assertFalse(self.settings(redis_url=None).continuous_game_enabled)
        with self.assertRaises(ValueError):
            self.settings(continuous_game_enabled=True, redis_url=None)
        enabled = self.settings(
            continuous_game_enabled=True, redis_url=SecretStr("redis://redis:6379/0")
        )
        self.assertTrue(enabled.continuous_game_enabled)

    def test_application_mounts_one_game_api_for_the_flag(self) -> None:
        code = (
            "import main; "
            "print(next(route.endpoint.__module__ for route in main.app.routes "
            "if getattr(route, 'path', '') == '/game/guess/'))"
        )

        def mounted(enabled: bool) -> str:
            environment = {
                **os.environ,
                "CONTINUOUS_GAME_ENABLED": str(enabled).lower(),
                "REDIS_URL": "redis://127.0.0.1:1/0",
            }
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=BACKEND, env=environment,
                capture_output=True, text=True, timeout=120, check=True,
            )
            return result.stdout.strip().splitlines()[-1]

        self.assertEqual(mounted(False), "app.game_api")
        self.assertEqual(mounted(True), "app.continuous_game_api")


if __name__ == "__main__":
    unittest.main()

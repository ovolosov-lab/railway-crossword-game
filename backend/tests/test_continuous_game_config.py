import os
import subprocess
import sys
import unittest
from pathlib import Path

from app.game_generation import create_redis_client

BACKEND = Path(__file__).resolve().parents[1]


class GameApplicationConfigurationTests(unittest.TestCase):
    def test_application_always_mounts_individual_session_api(self) -> None:
        code = (
            "import main; "
            "print(next(route.endpoint.__module__ for route in main.app.routes "
            "if getattr(route, 'path', '') == '/game/guess/'))"
        )
        environment = {
            **os.environ,
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "CONTINUOUS_GAME_ENABLED": "false",
        }
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=BACKEND,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )

        self.assertEqual(
            result.stdout.strip().splitlines()[-1],
            "app.continuous_game_api",
        )

    def test_game_generation_bootstrap_requires_redis(self) -> None:
        with self.assertRaisesRegex(ValueError, "REDIS_URL must be configured"):
            create_redis_client(None)


if __name__ == "__main__":
    unittest.main()

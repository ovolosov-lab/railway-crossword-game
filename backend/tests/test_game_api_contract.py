import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Query, status
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError

from app.api_errors import (
    ApiException,
    api_exception_handler,
    request_validation_exception_handler,
)
from app.config import Settings
from app.schemas import (
    ApiErrorResponse,
    CityRead,
    GameGuessRequest,
    GameHintRequest,
    GameRouteRequest,
    GameSyncQuery,
    HintProjection,
    GameGuessRequest,
    RoundProjection,
)
from app.security import (
    create_csrf_token,
    get_authenticated_user,
    get_current_user,
    is_valid_csrf_token,
    require_csrf_token,
)


class GameApiContractTests(unittest.TestCase):
    def test_round_id_is_flat_in_requests_and_nested_in_responses(self) -> None:
        round_id = uuid4()
        request = GameRouteRequest(round_id=round_id, route_ids=[1, 2])
        response = RoundProjection(id=round_id, status="active")

        self.assertEqual(request.model_dump()["round_id"], round_id)
        self.assertEqual(response.model_dump()["id"], round_id)

    def test_route_request_rejects_duplicate_route_ids(self) -> None:
        with self.assertRaises(ValidationError):
            GameRouteRequest(round_id=uuid4(), route_ids=[1, 1])

    def test_guess_and_hint_requests_validate_uuid_and_supported_types(self) -> None:
        round_id, placement_id = uuid4(), uuid4()
        guess = GameGuessRequest(
            round_id=round_id,
            placement_id=placement_id,
            answer=" ёлка ",
        )
        hint = GameHintRequest(
            round_id=round_id,
            placement_id=placement_id,
            hint_type="last_letter",
        )

        self.assertEqual(guess.answer, " ёлка ")
        self.assertEqual(hint.hint_type, "last_letter")
        with self.assertRaises(ValidationError):
            GameHintRequest(
                round_id=round_id,
                placement_id=placement_id,
                hint_type="unknown",
            )

    def test_sync_query_and_hint_response_follow_contract(self) -> None:
        query = GameSyncQuery(round_id=uuid4(), after_version=15)
        unavailable = HintProjection(
            hint_type="unknown",
            value="",
            points_charged=0,
            already_used=False,
        )

        self.assertEqual(query.after_version, 15)
        self.assertEqual(unavailable.hint_type, "unknown")
        with self.assertRaises(ValidationError):
            GameSyncQuery(round_id=uuid4(), after_version=-1)

    def test_city_schema_accepts_catalog_pixel_coordinates(self) -> None:
        city = CityRead(
            id=1,
            name="Лондон",
            country_code="GB",
            top=1405,
            left=779,
            is_capital=True,
        )
        self.assertEqual(city.top, 1405)

    def test_settings_validate_guess_sync_and_hint_configuration(self) -> None:
        secret = SecretStr("test-secret-for-game-api-contract-over-32-bytes")
        settings = Settings(
            jwt_secret=secret,
            user_guess_rate_limit=15,
            frontend_sync_interval=3.5,
            check_interval=20,
            game_duration_hours=6,
            hint_first_letter_cost=10,
            hint_last_letter_cost=20,
        )

        self.assertEqual(settings.user_guess_rate_limit, 15)
        self.assertEqual(settings.frontend_sync_interval, 3.5)
        for invalid_settings in (
            {"user_guess_rate_limit": 0},
            {"frontend_sync_interval": 0},
            {"check_interval": 0},
            {"game_duration_hours": 0},
            {"hint_first_letter_cost": -1},
            {"hint_last_letter_cost": -1},
        ):
            with self.subTest(settings=invalid_settings):
                with self.assertRaises(ValidationError):
                    Settings(jwt_secret=secret, **invalid_settings)

    def test_settings_read_rate_and_sync_limits_from_environment(self) -> None:
        secret = "test-secret-for-game-api-contract-over-32-bytes"
        with patch.dict(
            os.environ,
            {
                "JWT_SECRET": secret,
                "USER_GUESS_RATE_LIMIT": "12",
                "FRONTEND_SYNC_INTERVAL": "4.5",
                "CHECK_INTERVAL": "25",
                "GAME_DURATION_HOURS": "8",
                "HINT_FIRST_LETTER_COST": "11",
                "HINT_LAST_LETTER_COST": "21",
            },
            clear=True,
        ):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.user_guess_rate_limit, 12)
        self.assertEqual(settings.frontend_sync_interval, 4.5)
        self.assertEqual(settings.check_interval, 25)
        self.assertEqual(settings.game_duration_hours, 8)
        self.assertEqual(settings.hint_first_letter_cost, 11)
        self.assertEqual(settings.hint_last_letter_cost, 21)

    def test_error_response_schema_has_stable_envelope(self) -> None:
        error = ApiErrorResponse(
            error={
                "code": "route_not_connected",
                "message": "Chosen routes do not form a path.",
                "details": None,
            }
        )
        self.assertEqual(error.model_dump()["error"]["code"], "route_not_connected")

    def test_csrf_token_is_signed_and_tamper_evident(self) -> None:
        secret = "test-secret-for-game-api-contract-over-32-bytes"
        token = create_csrf_token(secret)

        self.assertTrue(is_valid_csrf_token(token, secret))
        self.assertFalse(is_valid_csrf_token(f"{token}x", secret))
        self.assertFalse(is_valid_csrf_token(token, "different-secret"))
        self.assertFalse(is_valid_csrf_token("malformed", secret))


class ApiSecurityEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = FastAPI(
            exception_handlers={
                StarletteHTTPException: api_exception_handler,
                RequestValidationError: request_validation_exception_handler,
            }
        )
        router = APIRouter()

        @router.get("/private", dependencies=[Depends(get_current_user)])
        async def private_endpoint() -> dict[str, str]:
            return {"status": "ok"}

        @router.post(
            "/private",
            dependencies=[
                Depends(get_current_user),
                Depends(require_csrf_token),
            ],
        )
        async def state_changing_endpoint() -> dict[str, str]:
            return {"status": "changed"}

        @router.get("/validated")
        async def validated_endpoint(
            count: int = Query(gt=0),
        ) -> dict[str, int]:
            return {"count": count}

        @router.post("/json")
        async def json_endpoint(payload: GameGuessRequest) -> dict[str, str]:
            return {"answer": payload.answer}

        @router.get("/limited")
        async def limited_endpoint() -> None:
            raise ApiException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="rate_limited",
                message="Guess limit exceeded.",
                headers={"Retry-After": "17"},
            )

        self.application.include_router(router)
        self.client = TestClient(self.application)

    def tearDown(self) -> None:
        self.client.close()
        self.application.dependency_overrides.clear()

    def test_unauthenticated_api_returns_json_401(self) -> None:
        async def no_user():
            return None

        self.application.dependency_overrides[get_authenticated_user] = no_user
        response = self.client.get("/private")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")
        self.assertNotIn("detail", response.json())

    def test_mutation_requires_csrf_cookie_and_matching_header(self) -> None:
        from app.models import User

        async def authenticated_user():
            return User(
                id=uuid4(),
                username="test-player",
                email="player@example.com",
                hashed_password="not-used",
                is_active=True,
            )

        self.application.dependency_overrides[get_authenticated_user] = (
            authenticated_user
        )
        with patch("app.security.get_settings") as settings_provider:
            settings_provider.return_value = Settings(
                jwt_secret=SecretStr(
                    "test-secret-for-game-api-contract-over-32-bytes"
                )
            )
            response = self.client.post("/private")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "csrf_validation_failed")

            token = create_csrf_token(
                "test-secret-for-game-api-contract-over-32-bytes"
            )
            mismatched_response = self.client.post(
                "/private",
                cookies={"csrf_token": token},
                headers={"X-CSRF-Token": create_csrf_token(
                    "test-secret-for-game-api-contract-over-32-bytes"
                )},
            )
            self.assertEqual(mismatched_response.status_code, 403)

            response = self.client.post(
                "/private",
                cookies={"csrf_token": token},
                headers={"X-CSRF-Token": token},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "changed"})

    def test_validation_errors_use_the_api_error_envelope(self) -> None:
        response = self.client.get("/validated?count=0")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "validation_error")
        self.assertNotIn("input", response.json()["error"]["details"][0])

    def test_malformed_json_is_reported_as_bad_request(self) -> None:
        response = self.client.post(
            "/json",
            content="{",
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "bad_request")

    def test_rate_limit_error_preserves_retry_after_header(self) -> None:
        response = self.client.get("/limited")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "rate_limited")
        self.assertEqual(response.headers["retry-after"], "17")


if __name__ == "__main__":
    unittest.main()

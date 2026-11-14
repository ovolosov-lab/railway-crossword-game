import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import get_db
from app.models import User
from app.security import get_authenticated_user
from main import app


class LoginPageTests(unittest.TestCase):
    def test_root_redirects_unauthenticated_user_to_login(self) -> None:
        with TestClient(app, follow_redirects=False) as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login/")

    def test_root_redirects_invalid_token_to_login(self) -> None:
        with TestClient(app, follow_redirects=False) as client:
            client.cookies.set("access_token", "invalid")
            response = client.get("/")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login/")

    def test_root_redirects_authenticated_user_to_game(self) -> None:
        user = User(
            id=uuid4(),
            username="railway-player",
            email="player@example.com",
            hashed_password="not-used-by-this-test",
            is_active=True,
        )
        session = AsyncMock()
        session.scalar.return_value = user

        async def override_get_db():
            return session

        app.dependency_overrides[get_db] = override_get_db
        try:
            with TestClient(app, follow_redirects=False) as client:
                client.cookies.set("access_token", create_access_token(user.id))
                response = client.get("/")
        finally:
            app.dependency_overrides.pop(get_db, None)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/game/")
        session.scalar.assert_awaited_once()

    def test_login_page_is_public_and_links_registration(self) -> None:
        with TestClient(app) as client:
            response = client.get("/login/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('type="email"', response.text)
        self.assertIn('type="password"', response.text)
        self.assertIn('href="/register/"', response.text)
        self.assertIn('action="/auth/"', response.text)
        self.assertIn('class="brand-mark" aria-hidden="true">🛤️</span>', response.text)

    def test_registration_page_is_public_and_has_required_fields(self) -> None:
        with TestClient(app) as client:
            response = client.get("/register/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/auth/register/"', response.text)
        self.assertIn('name="username"', response.text)
        self.assertIn('name="email"', response.text)
        self.assertIn('name="password"', response.text)
        self.assertIn('name="password_confirmation"', response.text)
        self.assertIn('href="/login/"', response.text)
        self.assertIn('class="brand-mark" aria-hidden="true">🛤️</span>', response.text)

    def test_login_assets_are_served(self) -> None:
        with TestClient(app) as client:
            script = client.get("/static/auth.js")
            stylesheet = client.get("/static/auth.css")

        self.assertEqual(script.status_code, 200)
        self.assertIn('credentials: "same-origin"', script.text)
        self.assertNotIn("localStorage", script.text)
        self.assertNotIn("sessionStorage", script.text)
        self.assertEqual(stylesheet.status_code, 200)

    def test_registration_script_is_served_with_client_validation(self) -> None:
        with TestClient(app) as client:
            script = client.get("/static/register.js")

        self.assertEqual(script.status_code, 200)
        self.assertIn("password !== confirmation", script.text)
        self.assertIn("new TextEncoder().encode(password).length > 72", script.text)
        self.assertIn('credentials: "same-origin"', script.text)
        self.assertNotIn("localStorage", script.text)
        self.assertNotIn("sessionStorage", script.text)

    def test_game_page_redirects_without_authentication_cookie(self) -> None:
        with TestClient(app, follow_redirects=False) as client:
            response = client.get("/game/")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login/")

    def test_game_page_redirects_with_invalid_authentication_cookie(self) -> None:
        with TestClient(app, follow_redirects=False) as client:
            response = client.get("/game/", cookies={"access_token": "invalid"})

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login/")

    def test_authenticated_game_page_loads_game_assets_and_polling_setting(self) -> None:
        """Render the map app only for an authenticated user."""
        user = User(
            id=uuid4(),
            username="map-player",
            email="map-player@example.com",
            hashed_password="not-used-by-this-test",
            is_active=True,
        )

        async def authenticated_user() -> User:
            return user

        app.dependency_overrides[get_authenticated_user] = authenticated_user
        client = TestClient(app)
        try:
            response = client.get("/game/")
            script = client.get("/static/game.js")
            stylesheet = client.get("/static/game.css")
            auth_stylesheet = client.get("/static/auth.css")
            confetti = client.get("/static/confetti.js")
            victory_audio = client.get("/static/victory.mp3")
            trophy = client.get("/static/img/mazer.svg")
            locomotive = client.get("/static/img/locomotive.gif")
        finally:
            client.close()
            app.dependency_overrides.pop(get_authenticated_user, None)

        self.assertEqual(response.status_code, 200)
        self.assertIn("map-player", response.text)
        self.assertIn('href="/static/game.css"', response.text)
        self.assertIn('src="/static/game.js"', response.text)
        self.assertIn('id="Layer_1" class="map-image"', response.text)
        self.assertNotIn('src="/static/img/map.svg"', response.text)
        self.assertIn('class="map-scale-control"', response.text)
        for scale in ("0.7", "0.85", "1"):
            self.assertIn(f'name="map-scale" value="{scale}"', response.text)
        self.assertIn('class="brand map-brand"', response.text)
        self.assertIn('class="brand-mark" aria-hidden="true">🛤️</span>', response.text)
        self.assertIn('class="game-user">Игрок: <strong>map-player</strong>', response.text)
        self.assertNotIn('id="map-caption"', response.text)
        self.assertNotIn('class="game-header"', response.text)
        self.assertIn('width="4000"', response.text)
        self.assertIn('height="3000"', response.text)
        self.assertIn('id="word-dialog"', response.text)
        self.assertIn('id="guess-form"', response.text)
        self.assertIn('src="/static/confetti.js"', response.text)
        self.assertIn('id="victory-train-overlay"', response.text)
        self.assertIn('id="victory-overlay"', response.text)
        self.assertIn('src="/static/img/mazer.svg"', response.text)
        self.assertIn('src="/static/img/locomotive.gif"', response.text)
        self.assertIn('id="victory-audio"', response.text)
        self.assertIn('src="/static/victory.mp3"', response.text)
        self.assertIn('class="victory-train-overlay"', response.text)
        self.assertRegex(response.text, r'name="csrf-cookie-name" content="[^"]+"')
        self.assertIn('data-sync-interval="', response.text)
        self.assertEqual(script.status_code, 200)
        self.assertIn('"/game/guess/"', script.text)
        self.assertIn('"/game/hint/"', script.text)
        self.assertIn("/game/sync/?", script.text)
        self.assertIn("app.dataset.syncInterval", script.text)
        self.assertIn("Number(source.left)", script.text)
        self.assertIn("Number(target.left)", script.text)
        self.assertIn("const cityClearance = 18 + 5 + 7 + 8", script.text)
        self.assertIn("const gap = Math.max(", script.text)
        self.assertIn("y: -1.5", script.text)
        self.assertIn("activePlacement()?.is_guessed", script.text)
        self.assertIn("if (!placement || placement.is_guessed) return;", script.text)
        self.assertIn("function startVictoryCelebration()", script.text)
        self.assertIn("function animateVictoryTrain(showTrophy)", script.text)
        self.assertIn("function showVictoryTrophy()", script.text)
        self.assertIn("function positionVictoryImages()", script.text)
        self.assertIn("elements.gamePanel.getBoundingClientRect()", script.text)
        self.assertIn("elements.mapScroll.getBoundingClientRect()", script.text)
        self.assertIn("segments.length * 1050", script.text)
        self.assertIn("state.shouldCelebrateVictory", script.text)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn("width: 4000px", stylesheet.text)
        self.assertIn("height: 3000px", stylesheet.text)
        self.assertIn("width: calc(100% - 32px)", stylesheet.text)
        self.assertIn("width: min(315px, 26.35%)", stylesheet.text)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)", stylesheet.text)
        self.assertIn("padding: 8px 0 36px", stylesheet.text)
        self.assertIn("animation: victory-trophy-bob", stylesheet.text)
        self.assertIn("width: clamp(120px, 14vw, 210px)", stylesheet.text)
        self.assertIn("height: 88vh", stylesheet.text)
        self.assertIn(".victory-locomotive", stylesheet.text)
        self.assertIn("display: none", stylesheet.text)
        self.assertIn("}, 8000)", script.text)
        self.assertEqual(auth_stylesheet.status_code, 200)
        self.assertIn("font-size: 28px", auth_stylesheet.text)
        self.assertEqual(confetti.status_code, 200)
        self.assertIn("global.confetti", confetti.text)
        self.assertIn("function renderFrame()", confetti.text)
        self.assertIn("animationFrame === null", confetti.text)
        self.assertEqual(victory_audio.status_code, 200)
        self.assertGreater(len(victory_audio.content), 0)
        self.assertEqual(trophy.status_code, 200)
        self.assertIn("<svg", trophy.text)
        self.assertEqual(locomotive.status_code, 200)
        self.assertGreater(len(locomotive.content), 0)


if __name__ == "__main__":
    unittest.main()

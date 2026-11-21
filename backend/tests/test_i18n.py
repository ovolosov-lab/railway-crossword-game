import asyncio
import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from app.api_errors import ApiException, api_exception_handler
from app.i18n import REQUIRED_KEYS, REQUIRED_SECTIONS, TranslationsError, load_translations
from app.game_messages import message_for
from main import templates


ROOT = Path(__file__).resolve().parents[2]


class LocalizationPayloadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_payload = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.in_payload = tag == "script" and attributes.get("id") == "i18n-data"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_payload = False

    def handle_data(self, data: str) -> None:
        if self.in_payload:
            self.parts.append(data)


class TranslationResourceTests(unittest.TestCase):
    def test_both_locale_files_have_complete_matching_sections(self) -> None:
        loaded = {language: load_translations(language) for language in ("ru", "en")}
        for language, translations in loaded.items():
            sections = {
                "html": translations.ui,
                "js": translations.js,
                "api": translations.api,
            }
            for section in REQUIRED_SECTIONS:
                with self.subTest(language=language, section=section):
                    self.assertTrue(REQUIRED_KEYS[section] <= sections[section].keys())
                    self.assertTrue(all(value.strip() for value in sections[section].values()))

        for section in REQUIRED_SECTIONS:
            self.assertEqual(
                set(getattr(loaded["ru"], {"html": "ui", "js": "js", "api": "api"}[section])),
                set(getattr(loaded["en"], {"html": "ui", "js": "js", "api": "api"}[section])),
            )

    def test_template_context_exposes_html_keys_and_keeps_the_localization_payload(self) -> None:
        translations = load_translations("en")
        context = translations.template_context()

        self.assertEqual(context["subtitle"], translations.ui["subtitle"])
        self.assertEqual(context["ui"], translations.ui)
        self.assertEqual(context["lang"], "en")
        self.assertEqual(context["translations"], translations.js)

    def test_locale_templates_render_translated_labels_and_json_payload(self) -> None:
        for language in ("ru", "en"):
            translations = load_translations(language)
            context = translations.template_context()
            context.update(
                username="locale-player",
                csrf_cookie_name="csrf_token",
                frontend_sync_interval=2,
                continuous_game_enabled=True,
            )
            for template_name in ("login.html", "register.html", "game.html"):
                with self.subTest(language=language, template=template_name):
                    rendered = templates.get_template(template_name).render(context)
                    self.assertIn(f'<html lang="{language}">', rendered)
                    self.assertNotIn("{{", rendered)
                    self.assertIn(translations.ui["game_title2"], rendered)
                    if template_name == "game.html":
                        self.assertIn('/static/game_sessions.js', rendered)
                        self.assertIn('id="surrender-button"', rendered)
                        self.assertIn(translations.ui["session_summary_label"], rendered)
                        confirm_button = rendered.split('id="confirm-route-button"', 1)[1].split(">", 1)[0]
                        self.assertIn("hidden", confirm_button)

                    parser = LocalizationPayloadParser()
                    parser.feed(rendered)
                    payload = json.loads("".join(parser.parts))
                    self.assertEqual(payload["lang"], language)
                    self.assertEqual(payload["js"]["word_length_one"], translations.js["word_length_one"])

    def test_game_template_keeps_the_mvp_client_available_when_sessions_are_disabled(self) -> None:
        context = load_translations("en").template_context()
        context.update(
            username="locale-player",
            csrf_cookie_name="csrf_token",
            frontend_sync_interval=2,
            continuous_game_enabled=False,
        )
        rendered = templates.get_template("game.html").render(context)
        self.assertIn('/static/game.js', rendered)
        self.assertNotIn('/static/game_sessions.js', rendered)
        confirm_button = rendered.split('id="confirm-route-button"', 1)[1].split(">", 1)[0]
        self.assertIn('style="display:none"', confirm_button)
        self.assertNotIn(" hidden", confirm_button)

    def test_missing_file_language_and_required_translation_fail_explicitly(self) -> None:
        with self.assertRaisesRegex(TranslationsError, "Unsupported language"):
            load_translations("de")

        with tempfile.TemporaryDirectory() as temporary_directory:
            locales = Path(temporary_directory)
            with self.assertRaisesRegex(TranslationsError, "Cannot read locale file"):
                load_translations("en", locales)

            resource = json.loads((ROOT / "locales" / "i18n_ru.json").read_text(encoding="utf-8"))
            del resource["html"]["subtitle"]
            (locales / "i18n_ru.json").write_text(
                json.dumps(resource, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(TranslationsError, "subtitle"):
                load_translations("ru", locales)

    def test_api_messages_follow_the_selected_language(self) -> None:
        for language, expected in (
            ("ru", "Для получения подсказки недостаточно очков."),
            ("en", "Not enough points to get a hint."),
        ):
            with self.subTest(language=language):
                self.assertEqual(message_for("insufficient_points", language), expected)
                request = SimpleNamespace(
                    app=SimpleNamespace(
                        state=SimpleNamespace(
                            game_settings=SimpleNamespace(language=language)
                        )
                    )
                )
                response = asyncio.run(
                    api_exception_handler(
                        request,
                        ApiException(
                            status_code=403,
                            code="insufficient_points",
                            message="unlocalized fallback",
                            continuous=True,
                        ),
                    )
                )
                body = json.loads(response.body)
                self.assertEqual(body["error"]["message"], expected)

    def test_game_script_does_not_contain_server_template_placeholders(self) -> None:
        for name in ("game.js", "game_sessions.js"):
            with self.subTest(script=name):
                script = (ROOT / "frontend" / "static" / name).read_text(encoding="utf-8")
                self.assertNotRegex(script, re.compile(r"\{\{\s*locale\s*\}\}"))
                self.assertIn("new Intl.PluralRules(locale)", script)


if __name__ == "__main__":
    unittest.main()

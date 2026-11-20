from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT

LOCALES_DIR = PROJECT_ROOT / "locales"
SUPPORTED_LANGUAGES = frozenset({"ru", "en"})
REQUIRED_SECTIONS = ("html", "js", "api")

REQUIRED_KEYS = {
    "html": frozenset(
        {
            "html_title", "subtitle", "game_title", "intro", "summary_label",
            "session_status", "game_loading", "loading", "statistics", "auto_route",
            "join_button", "confirm_route_button", "play_with_train", "leaderboard_title",
            "participant_count", "leaderboard_empty", "map_panel", "aria_label_main",
            "game_title2", "game_user", "map_help", "map_scroll", "aria_label_cities",
            "map_scroll_hint", "map_legend", "legend_traget", "map_scale", "victory_greet",
            "trophy", "the_word", "word_dialog_title", "close", "your_answer",
            "submit_guess", "hints", "first_hint", "last_hint", "login_page_title",
            "register_page_title", "brand_aria_label", "brand_alt", "login_eyebrow",
            "login_heading", "login_intro", "email_label", "password_label",
            "login_submit", "register_prompt", "register_link", "auth_about",
            "login_aside_kicker", "auth_aside_copy", "register_eyebrow",
            "register_heading", "register_intro", "username_label",
            "password_confirmation_label", "register_submit", "login_prompt",
            "login_link", "register_aside_kicker",
        }
    ),
    "js": frozenset(
        {
            "error_fetch", "error_response", "error_request", "status_unknown",
            "status_waiting", "status_active", "status_finished", "scores",
            "leaderboard_empty", "crossword_cells", "crossword_area_label", "city_aria_label",
            "route_aria_label", "line_aria_label", "word_dialog_guessed", "word_dialog_title",
            "hint_first", "hint_last", "message_won", "popup_message_sorry",
            "participants", "error_leaderboard", "error_sync", "hint_unavailable",
            "hint_denied", "checking", "already_solved", "incorrect", "too_much_attempts",
            "error_temporary_unavailable", "map_help_playing", "map_help_stat",
            "map_help_finished", "route_success", "route_select", "route_selected",
            "route_paths", "route_help", "watching_stats", "selecting_route", "playing",
            "help_watching_stats", "help_selecting_route", "help_playing", "start_city",
            "end_city", "reload_require", "session_finished", "help_route_start",
            "error_route", "route_confirmed", "error_words", "mess_game_finished",
            "error_confirm", "error_marshroot", "show_again", "deducted", "for_free",
            "correct", "answer", "join_deadline", "player_lost", "round_creating",
            "join_failed", "incomplete_round", "round_status_changed", "round_timed_out",
            "round_won_by_other", "word_length_one",
            "word_length_few", "word_length_many", "word_length_other", "registered_message",
            "login_submitting", "login_submit", "login_failed", "temporary_unavailable", "connection_failed",
            "username_invalid", "password_too_long", "password_mismatch",
            "register_submitting", "register_submit", "register_conflict", "validation_failed", "register_failed",
        }
    ),
    "api": frozenset(
        {
            "bad_request", "authentication_required", "forbidden", "not_found", "conflict",
            "validation_error", "rate_limited", "internal_error", "service_unavailable",
            "http_error", "admin_required", "csrf_validation_failed", "session_not_found",
            "placement_not_found", "route_not_found", "route_not_connected",
            "route_already_solved", "route_already_confirmed", "route_not_confirmed",
            "session_not_active", "no_available_route", "victory_finishing",
            "idempotency_key_conflict", "join_result_unavailable", "city_pairs_exhausted",
            "insufficient_points", "game_state_unavailable", "crossword_generation_failed",
            "city_catalog_unavailable", "city_pair_conflict", "crossword_unavailable",
            "dictionary_unavailable", "insufficient_score", "player_not_joined",
            "player_not_playing", "round_id_mismatch", "round_not_active", "round_unavailable",
            "stale_round", "route_word_conflict", "winner_persistence_failed",
            "word_not_available",
        }
    ),
}


class TranslationsError(RuntimeError):
    """Raised when a locale file is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Translations:
    """Localized UI and API strings for one supported application language."""

    language: str
    ui: dict[str, str]
    js: dict[str, str]
    api: dict[str, str]

    def template_context(self) -> dict[str, Any]:
        # Keep the namespace for consumers that need the complete map, while
        # exposing each UI key directly for the project's Jinja templates.
        return {
            "lang": self.language,
            "ui": self.ui,
            **self.ui,
            "translations": self.js,
        }


@lru_cache(maxsize=None)
def load_translations(language: str, locales_dir: Path = LOCALES_DIR) -> Translations:
    """Load and validate `i18n_<language>.json`; the result is cached per language."""
    if language not in SUPPORTED_LANGUAGES:
        raise TranslationsError(
            f"Unsupported language {language!r}; expected 'ru' or 'en'."
        )

    path = locales_dir / f"i18n_{language}.json"
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except OSError as exc:
        raise TranslationsError(f"Cannot read locale file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TranslationsError(f"Locale file {path} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise TranslationsError(f"Locale file {path} must contain a JSON object.")
    for section in REQUIRED_SECTIONS:
        values = data.get(section)
        if not isinstance(values, dict) or not all(
            isinstance(key, str)
            and isinstance(value, str)
            and value.strip()
            for key, value in values.items()
        ):
            raise TranslationsError(
                f"Locale file {path} must define section '{section}' as an object of strings."
            )

        missing = REQUIRED_KEYS[section].difference(values)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise TranslationsError(
                f"Locale file {path} is missing required {section} translations: {missing_list}."
            )

    return Translations(
        language=language,
        ui=data["html"],
        js=data["js"],
        api=data["api"],
    )

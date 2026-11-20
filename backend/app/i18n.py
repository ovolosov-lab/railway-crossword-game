from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT

LOCALES_DIR = PROJECT_ROOT / "locales"
REQUIRED_SECTIONS = ("html", "js")


class TranslationsError(RuntimeError):
    """Raised when a locale file is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Translations:
    """UI strings for one language: `ui` for templates, `js` for the browser."""

    language: str
    ui: dict[str, str]
    js: dict[str, str]

    def template_context(self) -> dict[str, Any]:
        return {"lang": self.language, "ui": self.ui, "translations": self.js}


@lru_cache(maxsize=None)
def load_translations(language: str, locales_dir: Path = LOCALES_DIR) -> Translations:
    """Load and validate `i18n_<language>.json`; the result is cached per language."""
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
            isinstance(key, str) and isinstance(value, str) for key, value in values.items()
        ):
            raise TranslationsError(
                f"Locale file {path} must define section '{section}' as an object of strings."
            )

    return Translations(language=language, ui=data["html"], js=data["js"])

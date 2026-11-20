"""Localized API messages loaded from the validated locale resources."""
from __future__ import annotations

from app.i18n import load_translations


def message_for(code: str, language: str) -> str:
    """Return a stable error code's message in the configured language."""
    return load_translations(language).api[code]

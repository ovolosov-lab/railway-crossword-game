"""Localized messages of the continuous-game API errors (ADR 0009, section 3).

Codes are stable; texts follow the configured ``LANGUAGE``.
"""
from __future__ import annotations

from typing import Literal

Language = Literal["ru", "en"]

MESSAGES: dict[str, dict[Language, str]] = {
    "session_not_found": {
        "ru": "Игровая сессия не найдена.",
        "en": "The game session was not found.",
    },
    "placement_not_found": {
        "ru": "Слово кроссворда не найдено.",
        "en": "The crossword word was not found.",
    },
    "route_not_found": {
        "ru": "Один или несколько выбранных участков не существуют.",
        "en": "One or more selected route segments do not exist.",
    },
    "route_not_connected": {
        "ru": "Выбранные участки не образуют непрерывный путь между назначенными городами.",
        "en": "The selected segments do not form a continuous path between the assigned cities.",
    },
    "route_already_solved": {
        "ru": "Маршрут уже разгадан. Выберите другой маршрут.",
        "en": "This route is already solved. Choose another route.",
    },
    "route_already_confirmed": {
        "ru": "Подтверждённый маршрут нельзя заменить.",
        "en": "A confirmed route cannot be replaced.",
    },
    "route_not_confirmed": {
        "ru": "Сначала подтвердите маршрут.",
        "en": "Confirm a route first.",
    },
    "session_not_active": {
        "ru": "Игровая сессия уже завершена.",
        "en": "The game session is already finished.",
    },
    "no_available_route": {
        "ru": "Не удалось подобрать доступный маршрут.",
        "en": "No available route could be found.",
    },
    "victory_finishing": {
        "ru": "Победа ещё сохраняется. Повторите попытку позже.",
        "en": "The victory is still being saved. Try again later.",
    },
    "idempotency_key_conflict": {
        "ru": "Ключ запроса уже использован с другими данными.",
        "en": "The request key was already used with different data.",
    },
    "join_result_unavailable": {
        "ru": "Результат прежнего присоединения больше недоступен.",
        "en": "The result of the earlier join is no longer available.",
    },
    "city_pairs_exhausted": {
        "ru": "Свободных пар городов не осталось. Попробуйте позже.",
        "en": "No free city pair is left. Try again later.",
    },
    "insufficient_points": {
        "ru": "Для получения подсказки недостаточно очков.",
        "en": "Not enough points to get a hint",
    },
    "rate_limited": {
        "ru": "Слишком много попыток. Повторите позже.",
        "en": "Too many attempts. Try again later.",
    },
    "game_state_unavailable": {
        "ru": "Игровое состояние временно недоступно.",
        "en": "The game state is temporarily unavailable.",
    },
    "crossword_generation_failed": {
        "ru": "Не удалось создать кроссворд. Присоединитесь к игре заново.",
        "en": "The crossword could not be created. Join the game again.",
    },
    "service_unavailable": {
        "ru": "Данные игры временно недоступны.",
        "en": "Game data is temporarily unavailable.",
    },
}


def message_for(code: str, language: Language) -> str:
    return MESSAGES[code][language]

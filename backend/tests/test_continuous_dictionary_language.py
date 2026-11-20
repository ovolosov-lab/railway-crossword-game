import unittest
from types import SimpleNamespace

from app.continuous_game_api import _load_candidates


class _DictionaryQuery:
    def __init__(self) -> None:
        self.language: str | None = None
        self.max_cells: int | None = None

    async def execute(self, statement):
        parameters = statement.compile().params
        self.language = parameters["language_code_1"]
        self.max_cells = parameters["letter_count_1"]
        rows = [
            (1, "слово", "Русская подсказка", "ru", True),
            (2, "train", "English clue", "en", True),
            (3, "longword", "Too long", "en", True),
        ]
        selected = [
            (identifier, word, clue)
            for identifier, word, clue, language, active in rows
            if language == self.language and active and len(word) <= self.max_cells
        ]
        return SimpleNamespace(all=lambda: selected)


class ContinuousDictionaryLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_query_selects_only_the_configured_language(self) -> None:
        for language, expected in (("ru", ["слово"]), ("en", ["train"])):
            with self.subTest(language=language):
                database = _DictionaryQuery()
                candidates = await _load_candidates(database, language, 6)

                self.assertEqual(database.language, language)
                self.assertEqual([candidate.word_text for candidate in candidates], expected)

    async def test_candidate_query_honors_capacity_for_both_languages(self) -> None:
        for language in ("ru", "en"):
            with self.subTest(language=language):
                database = _DictionaryQuery()
                candidates = await _load_candidates(database, language, 5)

                self.assertEqual(database.max_cells, 5)
                self.assertNotIn("longword", [candidate.word_text for candidate in candidates])


if __name__ == "__main__":
    unittest.main()

import unittest

from pydantic import ValidationError

from app.database import Base
from app.game_state import HintResult
from app.schemas import DictionaryWordClue, DictionaryWordCreate
import app.models  # noqa: F401


class DictionaryModelTests(unittest.TestCase):
    """Verify the ORM and schemas remain aligned with ADR 0006."""

    def test_dictionary_word_orm_matches_adr_0006(self) -> None:
        """Check the dictionary model exposes only its approved columns/index."""
        table = Base.metadata.tables["dictionary_words"]

        self.assertEqual(
            set(table.columns.keys()),
            {
                "id",
                "word_text",
                "definition",
                "language_code",
                "letter_count",
                "is_active",
                "created_at",
            },
        )
        self.assertEqual(
            set(Base.metadata.tables),
            {"users", "cities", "routes", "dictionary_words", "game_results"},
        )
        active_lookup = next(
            index
            for index in table.indexes
            if index.name == "ix_dictionary_words_active_lookup"
        )
        self.assertEqual(
            tuple(column.name for column in active_lookup.columns),
            ("language_code", "letter_count"),
        )

    def test_dictionary_schemas_reject_unknown_fields(self) -> None:
        """Ensure strict schemas reject any unsupported extra field."""
        with self.assertRaises(ValidationError):
            DictionaryWordCreate.model_validate(
                {
                    "word_text": "лес",
                    "definition": "Растительность",
                    "language_code": "ru",
                    "letter_count": 3,
                    "obsolete_field": 1,
                }
            )

        with self.assertRaises(ValidationError):
            DictionaryWordClue.model_validate(
                {
                    "id": 1,
                    "definition": "Растительность",
                    "language_code": "ru",
                    "letter_count": 3,
                    "obsolete_field": None,
                }
            )

    def test_unknown_hint_type_is_response_only(self) -> None:
        """Keep unavailable hints restricted to response models."""
        result = HintResult(
            hint_type="unknown",
            value="",
            points_charged=0,
            already_used=False,
            score_balance=0,
        )

        self.assertEqual(result.hint_type, "unknown")


if __name__ == "__main__":
    unittest.main()

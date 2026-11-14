import csv
import importlib.util
import unittest
from pathlib import Path

from sqlalchemy.dialects import postgresql


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "f4a8c21d903b_merge_latest_dictionary_words.py"
)
MIGRATION_SPEC = importlib.util.spec_from_file_location(
    "merge_latest_dictionary_words_migration",
    MIGRATION_PATH,
)
if MIGRATION_SPEC is None or MIGRATION_SPEC.loader is None:
    raise RuntimeError(f"Cannot load migration module at {MIGRATION_PATH}.")
MIGRATION = importlib.util.module_from_spec(MIGRATION_SPEC)
MIGRATION_SPEC.loader.exec_module(MIGRATION)


class DictionaryMergeMigrationTests(unittest.TestCase):
    def test_last_normalized_duplicate_occurrence_wins(self) -> None:
        rows, duplicate_occurrences = MIGRATION._read_word_catalog()
        pizza_key = MIGRATION._normalized_duplicate_key("пицца")
        pizza_occurrences: list[tuple[str, str]] = []
        for source_path in MIGRATION._word_files():
            with source_path.open(encoding="utf-8-sig", newline="") as source:
                for fields in csv.reader(source, delimiter=";"):
                    if len(fields) != 2:
                        continue
                    word_text, definition = (field.strip() for field in fields)
                    if MIGRATION._normalized_duplicate_key(word_text) == pizza_key:
                        pizza_occurrences.append((word_text, definition))

        self.assertGreater(len(pizza_occurrences), 1)
        imported = next(
            row
            for row in rows
            if MIGRATION._normalized_duplicate_key(str(row["word_text"]))
            == pizza_key
        )
        self.assertEqual(
            (imported["word_text"], imported["definition"]),
            pizza_occurrences[-1],
        )
        self.assertGreater(duplicate_occurrences, 0)

    def test_existing_word_conflict_updates_its_definition(self) -> None:
        statement = MIGRATION._word_upsert_statement(
            [
                {
                    "language_code": "ru",
                    "word_text": "пицца",
                    "definition": "Новое описание",
                    "letter_count": 5,
                }
            ],
            MIGRATION._dictionary_words_table(),
        )

        compiled = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn(
            "ON CONFLICT (language_code, word_text) DO UPDATE",
            compiled,
        )
        self.assertIn("definition = excluded.definition", compiled)


if __name__ == "__main__":
    unittest.main()

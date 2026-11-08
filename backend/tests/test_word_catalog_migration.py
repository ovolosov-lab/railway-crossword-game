import csv
import importlib.util
import unittest
from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "e8c42b7a19d3_seed_dictionary_words.py"
)
MIGRATION_SPEC = importlib.util.spec_from_file_location(
    "seed_dictionary_words_migration",
    MIGRATION_PATH,
)
if MIGRATION_SPEC is None or MIGRATION_SPEC.loader is None:
    raise RuntimeError(f"Cannot load migration module at {MIGRATION_PATH}.")
MIGRATION = importlib.util.module_from_spec(MIGRATION_SPEC)
MIGRATION_SPEC.loader.exec_module(MIGRATION)


class WordCatalogMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows, cls.skipped_duplicates = MIGRATION._read_word_catalog()

    def test_catalog_rows_are_valid_and_unique_after_normalization(self) -> None:
        keys: set[str] = set()
        for row in self.rows:
            word_text = str(row["word_text"])
            key = MIGRATION._normalized_duplicate_key(word_text)
            self.assertNotIn(key, keys)
            keys.add(key)
            self.assertEqual(row["language_code"], "ru")
            self.assertEqual(row["letter_count"], MIGRATION._letter_count(word_text))
            self.assertGreater(row["letter_count"], 0)
            self.assertLessEqual(len(word_text), 100)
            self.assertTrue(str(row["definition"]).strip())

    def test_numeric_file_names_are_sorted_and_part_numbers_are_unique(self) -> None:
        numbers = [int(path.stem) for path in MIGRATION._word_files()]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertIn(1, numbers)

    def test_first_duplicate_occurrence_is_kept(self) -> None:
        first_occurrence: tuple[str, str] | None = None
        duplicate_occurrences = 0
        seen_words: set[str] = set()
        total_duplicate_rows = 0
        for source_path in MIGRATION._word_files():
            with source_path.open(encoding="utf-8-sig", newline="") as source:
                for fields in csv.reader(source, delimiter=";"):
                    if len(fields) != 2:
                        continue
                    word_text, definition = (field.strip() for field in fields)
                    duplicate_key = MIGRATION._normalized_duplicate_key(word_text)
                    if duplicate_key in seen_words:
                        total_duplicate_rows += 1
                    else:
                        seen_words.add(duplicate_key)
                    if duplicate_key != MIGRATION._normalized_duplicate_key("пицца"):
                        continue
                    if first_occurrence is None:
                        first_occurrence = (word_text, definition)
                    else:
                        duplicate_occurrences += 1

        if first_occurrence is None:
            self.fail("Expected at least one occurrence of 'пицца'.")
        imported = next(
            row
            for row in self.rows
            if MIGRATION._normalized_duplicate_key(str(row["word_text"]))
            == MIGRATION._normalized_duplicate_key("пицца")
        )
        self.assertEqual(imported["word_text"], first_occurrence[0])
        self.assertEqual(imported["definition"], first_occurrence[1])
        self.assertGreater(duplicate_occurrences, 0)
        self.assertEqual(self.skipped_duplicates, total_duplicate_rows)

    def test_letter_count_includes_spaces_and_punctuation(self) -> None:
        self.assertEqual(MIGRATION._letter_count("Том-Ям"), 6)
        self.assertEqual(MIGRATION._letter_count("картошка фри"), 12)

    def test_catalog_covers_every_route_capacity(self) -> None:
        MIGRATION._validate_route_capacity_coverage(self.rows)

    def test_duplicate_normalization_matches_guess_equivalence(self) -> None:
        self.assertEqual(
            MIGRATION._normalized_duplicate_key("ЁЖ"),
            MIGRATION._normalized_duplicate_key("еж"),
        )


if __name__ == "__main__":
    unittest.main()

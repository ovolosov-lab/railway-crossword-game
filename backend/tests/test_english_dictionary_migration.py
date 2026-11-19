import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import DictionaryWord


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "72fd40c93b31_merge_english_dictionary.py"
)
SPEC = importlib.util.spec_from_file_location("english_dictionary_migration", MIGRATION_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load migration at {MIGRATION_PATH}")
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def run_migration(connection, action):
    with Operations.context(MigrationContext.configure(connection)):
        with patch.object(MIGRATION.context, "is_offline_mode", return_value=False):
            action()


class EnglishCatalogTests(unittest.TestCase):
    def test_real_catalog_and_normalized_duplicates(self) -> None:
        rows = MIGRATION._read_catalog()
        self.assertGreater(len(rows), 600)
        self.assertEqual(len(rows), len({MIGRATION._key(row["word_text"]) for row in rows}))
        self.assertIn("James Bond", [row["word_text"] for row in rows])
        self.assertEqual(MIGRATION._key(" É—X\u00a0Ё "), MIGRATION._key("e\u0301-xе"))
        self.assertNotEqual(MIGRATION._key("a.b"), MIGRATION._key("ab"))
        self.assertNotEqual(MIGRATION._key("a\u001cb"), MIGRATION._key("ab"))

    def test_last_duplicate_wins_and_invalid_input_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "versions").mkdir()
            catalog = root / "words" / "en"
            catalog.mkdir(parents=True)
            (root / "routes.json").write_text(
                json.dumps({"routes": [{"length_in_letters": 6}]}), encoding="utf-8"
            )
            files = {"1.csv": b' A-bc ;"first"\n', "2.csv": b'abc;"last"\n'}
            for name, data in files.items():
                (catalog / name).write_bytes(data)
            digests = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
            with patch.object(MIGRATION, "__file__", str(root / "versions" / "test.py")), patch.object(
                MIGRATION, "_FILES", digests
            ):
                rows = MIGRATION._read_catalog()
                self.assertEqual(rows, [{"word_text": "abc", "definition": "last", "letter_count": 3}])
                (catalog / "2.csv").unlink()
                with self.assertRaises(FileNotFoundError):
                    MIGRATION._read_catalog()
                (catalog / "2.csv").write_bytes(b"007;clue\n")
                digests["2.csv"] = hashlib.sha256(b"007;clue\n").hexdigest()
                with self.assertRaisesRegex(ValueError, "invalid word"):
                    MIGRATION._read_catalog()
                (catalog / "2.csv").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "has changed"):
                    MIGRATION._read_catalog()


@unittest.skipUnless(
    os.environ.get("RAILWAY_TEST_DATABASE_URL"),
    "Set RAILWAY_TEST_DATABASE_URL to a disposable PostgreSQL database.",
)
class EnglishDictionaryPostgreSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.schema = f"test_en_{uuid4().hex}"
        self.admin = create_async_engine(os.environ["RAILWAY_TEST_DATABASE_URL"])
        async with self.admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(
            os.environ["RAILWAY_TEST_DATABASE_URL"],
            connect_args={"server_settings": {"search_path": self.schema}},
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(DictionaryWord.__table__.create)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        async with self.admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.admin.dispose()

    async def test_merge_and_repeated_upgrade_downgrade_preserve_existing(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(text(
                "INSERT INTO dictionary_words (word_text, definition, language_code, letter_count, is_active) "
                "VALUES ('Борщ', 'русский', 'ru', 4, true), "
                "(' BORSCHT ', 'old clue', 'en', 9, false)"
            ))
            original = (await connection.execute(text(
                "SELECT id, word_text, letter_count, is_active, created_at "
                "FROM dictionary_words WHERE language_code = 'en'"
            ))).one()
            await connection.run_sync(run_migration, MIGRATION.upgrade)
            current = (await connection.execute(text(
                "SELECT id, word_text, letter_count, is_active, created_at "
                "FROM dictionary_words WHERE language_code = 'en' AND id = :id"
            ), {"id": original.id})).one()
            self.assertEqual(current, original)
            self.assertNotEqual(await connection.scalar(text(
                "SELECT definition FROM dictionary_words WHERE id = :id"
            ), {"id": original.id}), "old clue")
            self.assertEqual(await connection.scalar(text(
                "SELECT definition FROM dictionary_words WHERE language_code = 'ru'"
            )), "русский")
            count = await connection.scalar(text(
                "SELECT count(*) FROM dictionary_words WHERE language_code = 'en'"
            ))
            self.assertEqual(count, len(MIGRATION._read_catalog()))
            await connection.run_sync(run_migration, MIGRATION.downgrade)
            self.assertEqual(await connection.scalar(text(
                "SELECT definition FROM dictionary_words WHERE id = :id"
            ), {"id": original.id}), "old clue")
            self.assertEqual(await connection.scalar(text(
                "SELECT count(*) FROM dictionary_words"
            )), 2)
            await connection.run_sync(run_migration, MIGRATION.upgrade)
            self.assertEqual(await connection.scalar(text(
                "SELECT count(*) FROM dictionary_words WHERE language_code = 'en'"
            )), count)

    async def test_ambiguous_existing_words_abort_without_changes(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(text(
                "INSERT INTO dictionary_words (word_text, definition, language_code, letter_count) "
                "VALUES ('Borscht', 'one', 'en', 7), ('BORSCHT', 'two', 'en', 7)"
            ))
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            async with self.engine.begin() as connection:
                await connection.run_sync(run_migration, MIGRATION.upgrade)
        async with self.engine.connect() as connection:
            self.assertEqual(await connection.scalar(text(
                "SELECT count(*) FROM dictionary_words"
            )), 2)

    async def test_downgrade_rejects_later_edits_and_references(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(run_migration, MIGRATION.upgrade)
            word_id = await connection.scalar(text(
                "SELECT min(id) FROM dictionary_words WHERE language_code = 'en'"
            ))
            await connection.execute(text(
                "UPDATE dictionary_words SET definition = 'later edit' WHERE id = :id"
            ), {"id": word_id})
        with self.assertRaisesRegex(RuntimeError, "was changed"):
            async with self.engine.begin() as connection:
                await connection.run_sync(run_migration, MIGRATION.downgrade)
        async with self.engine.begin() as connection:
            await connection.execute(text(
                f"UPDATE dictionary_words w SET definition = b.definition FROM {MIGRATION._BACKUP} b "
                "WHERE w.id = b.id AND w.id = :id"
            ), {"id": word_id})
            await connection.execute(text(
                "CREATE TABLE word_refs (word_id integer REFERENCES dictionary_words(id))"
            ))
            await connection.execute(text("INSERT INTO word_refs VALUES (:id)"), {"id": word_id})
        with self.assertRaises(IntegrityError):
            async with self.engine.begin() as connection:
                await connection.run_sync(run_migration, MIGRATION.downgrade)
        async with self.engine.connect() as connection:
            self.assertEqual(await connection.scalar(text(
                "SELECT count(*) FROM dictionary_words"
            )), len(MIGRATION._read_catalog()))


if __name__ == "__main__":
    unittest.main()

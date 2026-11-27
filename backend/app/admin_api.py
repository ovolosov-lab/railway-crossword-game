from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from loguru import logger
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_errors import ApiException
from app.database import get_db
from app.models import DictionaryWord, User
from app.schemas import DictionaryImportRequest, DictionaryImportResponse
from app.security import get_current_user, get_current_user_from_header


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_user)],
)
dict_file_router = APIRouter(tags=["admin"])

_WORDS_DIRECTORY = Path(__file__).resolve().parents[1] / "migrations" / "words"
_CSV_FILENAME = re.compile(r"^[A-Za-z0-9_-]+\.csv$")


@router.post(
    "/dictionary/",
    response_model=DictionaryImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add dictionary words",
)
async def add_dictionary_words(
    payload: DictionaryImportRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> DictionaryImportResponse:
    if user.username != "administrator":
        raise ApiException(
            status_code=status.HTTP_403_FORBIDDEN,
            code="admin_required",
            message="Only the admin user can add dictionary words.",
        )

    words = [
        {
            "word_text": item.word,
            "definition": item.description,
            "language_code": item.language,
            "letter_count": len(item.word),
        }
        for item in payload.dictionary
    ]
    statement = (
        insert(DictionaryWord)
        .values(words)
        .on_conflict_do_nothing(
            index_elements=[
                DictionaryWord.language_code,
                DictionaryWord.word_text,
            ]
        )
    )

    try:
        result = await session.execute(statement)
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        logger.exception("Could not add dictionary words")
        raise ApiException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="service_unavailable",
            message="Dictionary words could not be added at this time.",
        ) from exc

    return DictionaryImportResponse(added=result.rowcount or 0) # pyright: ignore[reportAttributeAccessIssue]


@router.post(
    "/dict_file/",
    response_model=DictionaryImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import dictionary words from a server CSV file",
)
async def add_dictionary_words_from_file(
    filename: Annotated[str, Query(min_length=5, max_length=80)],
    language: Annotated[Literal["ru", "en"], Query()],
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> DictionaryImportResponse:
    if user.username != "administrator":
        raise ApiException(
            status_code=status.HTTP_403_FORBIDDEN,
            code="admin_required",
            message="Only the admin user can add dictionary words.",
        )

    if not _CSV_FILENAME.fullmatch(filename):
        raise ApiException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="bad_request",
            message="The filename must be a CSV file name without a directory path.",
        )

    words_directory = _WORDS_DIRECTORY / ("en" if language == "en" else "")
    resolved_directory = words_directory.resolve()
    path = (resolved_directory / filename).resolve()
    if path.parent != resolved_directory:
        raise ApiException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="bad_request",
            message="The filename must be a CSV file name without a directory path.",
        )
    if not path.is_file():
        raise ApiException(
            status_code=status.HTTP_404_NOT_FOUND,
            code="not_found",
            message="The requested dictionary file was not found.",
        )

    words: list[dict[str, str | int]] = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(
                source,
                delimiter=";",
                skipinitialspace=True,
                strict=True,
            )
            for line_number, fields in enumerate(reader, start=1):
                if len(fields) != 2:
                    raise ValueError(
                        f"Line {line_number} must contain two semicolon-separated columns."
                    )
                word = unicodedata.normalize("NFC", fields[0].strip())
                definition = fields[1].strip()
                if not word or len(word) > 100:
                    raise ValueError(
                        f"Line {line_number} must contain a word of 1 to 100 characters."
                    )
                if not definition:
                    raise ValueError(f"Line {line_number} has an empty clue.")
                words.append(
                    {
                        "word_text": word,
                        "definition": definition,
                        "language_code": language,
                        "letter_count": len(word),
                    }
                )
    except UnicodeDecodeError as exc:
        raise ApiException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="bad_request",
            message="The dictionary file must use UTF-8 encoding.",
        ) from exc
    except (OSError, csv.Error, ValueError) as exc:
        raise ApiException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="bad_request",
            message=f"Could not parse the dictionary CSV: {exc}",
        ) from exc

    if not words:
        raise ApiException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="bad_request",
            message="The dictionary file is empty.",
        )

    statement = (
        insert(DictionaryWord)
        .values(words)
        .on_conflict_do_nothing(
            index_elements=[
                DictionaryWord.language_code,
                DictionaryWord.word_text,
            ]
        )
    )
    try:
        result = await session.execute(statement)
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        logger.exception("Could not import dictionary words from {}", filename)
        raise ApiException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="service_unavailable",
            message="Dictionary words could not be added at this time.",
        ) from exc

    return DictionaryImportResponse(added=result.rowcount or 0) # pyright: ignore[reportAttributeAccessIssue]

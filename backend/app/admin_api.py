from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from loguru import logger
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_errors import ApiException
from app.database import get_db
from app.models import DictionaryWord, User
from app.schemas import DictionaryImportRequest, DictionaryImportResponse
from app.security import get_current_user


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_user)],
)


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
    if user.username != "admin":
        raise ApiException(
            status_code=status.HTTP_403_FORBIDDEN,
            code="admin_required",
            message="Only the admin user can add dictionary words.",
        )

    words = [
        {
            "word_text": item.word,
            "definition": item.description,
            "language_code": "ru",
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

    return DictionaryImportResponse(added=result.rowcount or 0)

from __future__ import annotations

import unicodedata
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

LanguageCode = Annotated[
    str,
    Field(
        min_length=2,
        max_length=5,
        pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2})?$",
    ),
]
PositiveId = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
HintType = Literal["first_letter", "last_letter"]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class UserRegister(Schema):
    username: str = Field(min_length=1, max_length=32)
    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=8, max_length=72)

    @field_validator("username", mode="before")
    @classmethod
    def strip_username(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("password")
    @classmethod
    def enforce_bcrypt_byte_limit(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) > 72:
            raise ValueError("Password must not exceed 72 UTF-8 bytes.")
        return value


class UserLogin(Schema):
    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=1, max_length=72)

    @field_validator("password")
    @classmethod
    def enforce_bcrypt_byte_limit(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) > 72:
            raise ValueError("Password must not exceed 72 UTF-8 bytes.")
        return value


class UserRead(Schema):
    id: UUID
    username: str = Field(min_length=1, max_length=32)
    email: EmailStr = Field(max_length=254)
    score: Annotated[int, Field(ge=0)]
    is_active: bool
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CityRead(Schema):
    id: PositiveId
    name: str = Field(min_length=1, max_length=120)
    country_code: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    top: Annotated[Decimal, Field(ge=0, max_digits=6, decimal_places=2)]
    left: Annotated[Decimal, Field(ge=0, max_digits=6, decimal_places=2)]
    is_capital: bool


class RouteRead(Schema):
    id: PositiveId
    source_city_id: PositiveId
    target_city_id: PositiveId
    length_in_letters: Annotated[int, Field(gt=0, le=32767)]

    @model_validator(mode="after")
    def endpoints_must_differ(self) -> RouteRead:
        if self.source_city_id == self.target_city_id:
            raise ValueError("A route must connect two different cities.")
        return self


class DictionaryWordCreate(Schema):
    word_text: str = Field(min_length=1, max_length=100)
    definition: str = Field(min_length=1)
    language_code: LanguageCode
    letter_count: Annotated[int, Field(gt=0, le=32767)]
    is_active: bool = True

    @field_validator("word_text", "definition", mode="before")
    @classmethod
    def strip_required_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class DictionaryImportWord(Schema):
    word: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1)

    @field_validator("word", mode="before")
    @classmethod
    def normalize_word(cls, value: object) -> object:
        return unicodedata.normalize("NFC", value.strip()) if isinstance(value, str) else value

    @field_validator("description", mode="before")
    @classmethod
    def strip_description(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class DictionaryImportRequest(Schema):
    dictionary: list[DictionaryImportWord] = Field(min_length=1)


class DictionaryImportResponse(Schema):
    added: NonNegativeInt


class DictionaryWordClue(Schema):
    id: PositiveId
    definition: str
    language_code: LanguageCode
    letter_count: Annotated[int, Field(gt=0, le=32767)]


class LeaderboardEntry(Schema):
    username: str = Field(min_length=1, max_length=32)
    score: NonNegativeInt


class ErrorProjection(Schema):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    details: dict[str, object] | list[dict[str, object]] | None = None


class ApiErrorResponse(Schema):
    error: ErrorProjection

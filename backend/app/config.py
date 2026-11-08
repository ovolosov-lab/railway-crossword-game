from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    app_name: str = "Railway"
    app_env: str = "development"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    database_url: str | None = None
    db_user: str | None = None
    db_password: str | None = None
    db_host: str | None = None
    db_port: int | None = Field(default=None, ge=1, le=65535)
    db_name: str | None = None
    jwt_secret: SecretStr = Field(default=SecretStr(""), repr=False)
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_expire_minutes: int = Field(default=120, ge=1)
    auth_cookie_name: str = "access_token"
    csrf_cookie_name: str = "csrf_token"
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    game_duration_hours: int = Field(default=24, ge=1)
    check_interval: int = Field(default=40, ge=1)
    user_guess_rate_limit: int = Field(default=30, ge=1)
    frontend_sync_interval: float = Field(default=2.0, gt=0)
    hint_first_letter_cost: int = Field(default=20, ge=0)
    hint_last_letter_cost: int = Field(default=30, ge=0)
    log_file: Path = PROJECT_ROOT / "logs" / "app.log"
    log_rotation: str = "12 MB"
    log_retention: str = "2 days"

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret_strength(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 UTF-8 bytes.")
        return value

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url:
            return self.database_url

        components = (
            self.db_user,
            self.db_password,
            self.db_host,
            self.db_port,
            self.db_name,
        )
        if any(component is None for component in components):
            raise ValueError(
                "Set DATABASE_URL or all of DB_USER, DB_PASSWORD, DB_HOST, "
                "DB_PORT, and DB_NAME."
            )

        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        ).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()

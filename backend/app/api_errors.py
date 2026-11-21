from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.i18n import load_translations
from app.schemas import ApiErrorResponse, ErrorProjection


class ApiException(HTTPException):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | list[dict[str, Any]] | None = None,
        player_projection: dict[str, Any] | None = None,
        session_projection: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.details = details
        self.player_projection = player_projection
        self.session_projection = session_projection


_ERROR_CODES_BY_STATUS = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "authentication_required",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "validation_error",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "internal_error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | list[dict[str, Any]] | None = None,
    player_projection: dict[str, Any] | None = None,
    session_projection: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    content: dict[str, Any] = jsonable_encoder(
        ApiErrorResponse(error=ErrorProjection(code=code, message=message, details=details))
    )
    if session_projection is not None:
        content["session"] = session_projection
    if player_projection is not None:
        content["player"] = player_projection
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers=headers,
    )


async def api_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    settings = getattr(request.app.state, "game_settings", None) or get_settings()
    api_messages = load_translations(settings.language).api
    if isinstance(exc, ApiException):
        return _error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=api_messages.get(exc.code, str(exc.detail)),
            details=exc.details,
            player_projection=exc.player_projection,
            session_projection=exc.session_projection,
            headers=exc.headers,
        )

    code = _ERROR_CODES_BY_STATUS.get(exc.status_code, "http_error")
    return _error_response(
        status_code=exc.status_code,
        code=code,
        message=api_messages.get(code, str(exc.detail)),
        headers=exc.headers,
    )


async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    settings = getattr(request.app.state, "game_settings", None) or get_settings()
    validation_message = load_translations(settings.language).api["validation_error"]
    validation_errors = exc.errors()
    malformed_json = any(error.get("type") == "json_invalid" for error in validation_errors)
    status_code = (
        status.HTTP_400_BAD_REQUEST
        if malformed_json
        else status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    details = [
        {
            "location": [str(part) for part in error.get("loc", ())],
            "message": validation_message,
            "type": str(error.get("type", "value_error")),
        }
        for error in validation_errors
    ]
    return _error_response(
        status_code=status_code,
        code=_ERROR_CODES_BY_STATUS[status_code],
        message=validation_message,
        details=details,
    )

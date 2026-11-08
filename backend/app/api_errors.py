from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas import ApiErrorResponse, ErrorProjection


class ApiException(HTTPException):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | list[dict[str, Any]] | None = None,
        round_projection: dict[str, Any] | None = None,
        player_projection: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.details = details
        self.round_projection = round_projection
        self.player_projection = player_projection


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
    round_projection: dict[str, Any] | None = None,
    player_projection: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    response_model = ApiErrorResponse(
        error=ErrorProjection(code=code, message=message, details=details),
        round=round_projection,
        player=player_projection,
    )
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(response_model),
        headers=headers,
    )


async def api_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    del request
    if isinstance(exc, ApiException):
        return _error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=str(exc.detail),
            details=exc.details,
            round_projection=exc.round_projection,
            player_projection=exc.player_projection,
            headers=exc.headers,
        )

    return _error_response(
        status_code=exc.status_code,
        code=_ERROR_CODES_BY_STATUS.get(exc.status_code, "http_error"),
        message=str(exc.detail),
        headers=exc.headers,
    )


async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    del request
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
            "message": str(error.get("msg", "Invalid value")),
            "type": str(error.get("type", "value_error")),
        }
        for error in validation_errors
    ]
    return _error_response(
        status_code=status_code,
        code=_ERROR_CODES_BY_STATUS[status_code],
        message="Request body or query parameters are invalid.",
        details=details,
    )

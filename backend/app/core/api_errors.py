"""Error shape of the public /v1 API: {"error": {"code": "...", "message": "..."}}.

Codes are stable identifiers clients can switch on; messages are for humans.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

PUBLIC_PREFIX = "/v1"


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content=error_body(exc.code, exc.message), headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith(PUBLIC_PREFIX):
            first = exc.errors()[0] if exc.errors() else {}
            where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
            message = f"{where}: {first.get('msg', 'invalid request')}" if where else "invalid request"
            return JSONResponse(status_code=422, content=error_body("invalid_request", message))
        # Dashboard routes keep FastAPI's default {"detail": [...]} shape.
        from fastapi.encoders import jsonable_encoder

        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

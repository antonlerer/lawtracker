from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.fields = status, code, message, fields or {}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError):
        body = {"error": {"code": exc.code, "message": exc.message, "fields": exc.fields}}
        return JSONResponse(body, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        fields = {}
        for e in exc.errors():
            loc = [str(x) for x in e["loc"] if x not in ("body", "query")]
            fields[".".join(loc) or "_"] = e["msg"].removeprefix("Value error, ")
        body = {"error": {"code": "validation_error", "message": "Проверьте введённые данные", "fields": fields}}
        return JSONResponse(body, status_code=422)

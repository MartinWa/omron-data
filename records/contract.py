import json
from datetime import UTC, datetime

from django.http import HttpResponse

CONTRACT_VERSION = "2.0"
API_PREFIX = "/api/v1/"
MAX_LIMIT = 1000
DEFAULT_LIMIT = 100
MAX_RANGE_DAYS = 1830

STATUS_CODES = {
    400: ("bad_request", "Bad request"),
    401: ("invalid_credentials", "A valid bearer token is required"),
    403: ("permission_denied", "Permission denied"),
    404: ("not_found", "Resource not found"),
    405: ("method_not_allowed", "Method not allowed"),
    422: ("validation_error", "Validation failed"),
    500: ("internal_error", "Internal server error"),
    503: ("service_unavailable", "Service temporarily unavailable"),
}


def is_api_path(path: str) -> bool:
    return path == API_PREFIX.rstrip("/") or path.startswith(API_PREFIX)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def envelope() -> dict:
    return {"contract_version": CONTRACT_VERSION, "generated_at": utc_now_iso()}


def error_payload(code: str, message: str, details=None) -> dict:
    return {
        **envelope(),
        "error": {"code": code, "message": message, "details": details},
    }


def error_response(status: int, code: str, message: str, details=None) -> HttpResponse:
    response = HttpResponse(
        json.dumps(error_payload(code, message, details)),
        status=status,
        content_type="application/json",
    )
    if status == 401:
        response["WWW-Authenticate"] = "Bearer"
    return response

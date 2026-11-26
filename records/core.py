import logging

from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import Http404
from ninja import NinjaAPI
from ninja.errors import HttpError, ValidationError
from ninja.schema import NinjaGenerateJsonSchema
from pydantic.json_schema import models_json_schema

from .auth import ApiTokenAuth
from .contract import CONTRACT_VERSION, STATUS_CODES, ApiError, error_response
from .schemas import ErrorOut

logger = logging.getLogger("django")

_DOCUMENTED_ERRORS = {status: message for status, (_, message) in STATUS_CODES.items()}


def _document_error_responses(schema):
    refs, definitions = models_json_schema(
        [(ErrorOut, "serialization")],
        ref_template="#/components/schemas/{model}",
        schema_generator=NinjaGenerateJsonSchema,
    )
    schema["components"]["schemas"].update(definitions.get("$defs", {}))
    content = {"application/json": {"schema": refs[(ErrorOut, "serialization")]}}
    for methods in schema["paths"].values():
        for operation in methods.values():
            statuses = dict(_DOCUMENTED_ERRORS)
            if "security" not in operation:
                statuses.pop(401, None)
            responses = operation.setdefault("responses", {})
            for status, description in statuses.items():
                responses.setdefault(status, {"description": description, "content": content})


class DocumentedNinjaAPI(NinjaAPI):
    def get_openapi_schema(self, *, path_prefix=None, path_params=None):
        schema = super().get_openapi_schema(path_prefix=path_prefix, path_params=path_params)
        _document_error_responses(schema)
        return schema


def _from_status(status, details=None):
    code, message = STATUS_CODES.get(status, ("error", "Request failed"))
    return error_response(status, code, message, details)


api = DocumentedNinjaAPI(
    title="omron-data API",
    version=CONTRACT_VERSION,
    description="Read-only, owner-scoped OMRON connect measurement history.",
    openapi_url="/openapi.json",
    docs_url="/docs",
    urls_namespace="omron",
    auth=ApiTokenAuth(),
)


def _handle_api_error(request, exc):
    return error_response(exc.status, exc.code, exc.message, exc.details)


def _handle_http_error(request, exc):
    return _from_status(exc.status_code)


def _handle_validation(request, exc):
    return error_response(422, "validation_error", "Validation failed", exc.errors)


def _handle_404(request, exc):
    return _from_status(404)


def _handle_permission(request, exc):
    return _from_status(403)


def _handle_suspicious(request, exc):
    return _from_status(400)


def _handle_unexpected(request, exc):
    logger.exception("Unhandled API error", exc_info=exc)
    return _from_status(500)


api.add_exception_handler(ApiError, _handle_api_error)
api.add_exception_handler(HttpError, _handle_http_error)
api.add_exception_handler(ValidationError, _handle_validation)
api.add_exception_handler(Http404, _handle_404)
api.add_exception_handler(PermissionDenied, _handle_permission)
api.add_exception_handler(SuspiciousOperation, _handle_suspicious)
api.add_exception_handler(Exception, _handle_unexpected)

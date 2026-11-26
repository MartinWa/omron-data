import re
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode

from ninja.params.models import Query as QueryField
from ninja.params.models import QueryModel
from pydantic import BeforeValidator, ConfigDict

from .contract import DEFAULT_LIMIT, MAX_LIMIT, MAX_RANGE_DAYS, ApiError

_INT_RE = re.compile(r"[+-]?\d+", re.ASCII)
_DATETIME_ERROR = "must be an ISO-8601 datetime with a UTC offset"
MAX_OFFSET = 2**31 - 1


def _strict_int(value):
    if isinstance(value, bool):
        raise ValueError("must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INT_RE.fullmatch(value):
        return int(value)
    raise ValueError("must be an integer")


def _strict_flag(value):
    if isinstance(value, bool):
        return value
    if value in ("true", "components"):
        return True
    if value == "false":
        return False
    raise ValueError("must be true, false or components")


def _strict_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(_DATETIME_ERROR) from None
    else:
        raise ValueError(_DATETIME_ERROR)
    if parsed.tzinfo is None:
        raise ValueError(_DATETIME_ERROR)
    return parsed.astimezone(UTC)


def _empty_to_none(value):
    if value == "":
        return None
    return value


IntParam = Annotated[int, BeforeValidator(_strict_int)]
OptFlagParam = Annotated[bool | None, BeforeValidator(_strict_flag)]
OptFilterParam = Annotated[str | None, BeforeValidator(_empty_to_none)]
OptDateTimeParam = Annotated[datetime | None, BeforeValidator(_strict_datetime)]


class StrictQueryModel(QueryModel):
    model_config = ConfigDict(extra="forbid")

    @classmethod
    def get_request_data(cls, request, api, path_params):
        data = super().get_request_data(request, api, path_params)
        allowed = {field.alias or name for name, field in cls.model_fields.items()}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ApiError(
                422,
                "validation_error",
                "Unknown query parameter: " + ", ".join(unknown),
                details=unknown,
            )
        collection_fields = getattr(cls, "__ninja_collection_fields__", None) or []
        for key in data:
            if key not in collection_fields and len(request.GET.getlist(key)) > 1:
                raise ApiError(
                    422,
                    "validation_error",
                    "Repeated query parameter: " + key,
                    details=[key],
                )
        return data


class StrictQuery(QueryField):
    _model = StrictQueryModel

    @classmethod
    def _param_source(cls) -> str:
        return "query"


class NoQuery(StrictQueryModel):
    pass


NO_VALUE = StrictQuery(None)
LIMIT_QUERY = StrictQuery(DEFAULT_LIMIT)
OFFSET_QUERY = StrictQuery(0)


def validate_pagination(limit, offset):
    if limit < 1 or limit > MAX_LIMIT:
        raise ApiError(422, "validation_error", f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise ApiError(422, "validation_error", "offset must be non-negative")
    if offset > MAX_OFFSET:
        raise ApiError(422, "validation_error", f"offset must be at most {MAX_OFFSET}")


def validate_range(since, until):
    if since is None or until is None:
        return
    if since > until:
        raise ApiError(422, "validation_error", "since must be less than or equal to until")
    if until - since > timedelta(days=MAX_RANGE_DAYS):
        raise ApiError(
            422,
            "validation_error",
            f"range may not exceed {MAX_RANGE_DAYS} days",
        )


def sort_query(keys, default):
    allowed = ", ".join(sorted(keys))
    return StrictQuery(
        None,
        description=(
            "Sort key, optionally prefixed with '-' for descending order. "
            f"Allowed keys: {allowed}. Default: {default}. "
            "Ties are always broken by id ascending."
        ),
    )


def parse_sort(value, allowed, default):
    value = value or default
    descending = value.startswith("-")
    key = value[1:] if descending else value
    if key not in allowed:
        raise ApiError(422, "validation_error", f"Unsupported sort key: {key}")
    return key, descending


def build_page(request, items, count, limit, offset):
    has_more = offset + limit < count
    has_previous = 0 < offset <= count

    def continuation(new_offset):
        if request is None:
            return None
        params = {key: request.GET.getlist(key) for key in request.GET}
        params.pop("offset", None)
        params.setdefault("limit", [str(limit)])
        params["offset"] = [str(new_offset)]
        return request.path + "?" + urlencode(params, doseq=True)

    return {
        "items": items,
        "count": count,
        "next": continuation(offset + limit) if has_more else None,
        "previous": continuation(max(0, offset - limit)) if has_previous else None,
        "has_more": has_more,
    }

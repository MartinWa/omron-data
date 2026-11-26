from ninja import Router

from ..contract import envelope
from ..params import LIMIT_QUERY, OFFSET_QUERY, IntParam, validate_pagination
from ..queries import imports_data
from ..schemas import ImportsOut

router = Router()


@router.get("/imports", response=ImportsOut, tags=["imports"], summary="Import run history")
def imports(
    request,
    limit: IntParam = LIMIT_QUERY,
    offset: IntParam = OFFSET_QUERY,
):
    validate_pagination(limit, offset)
    page = imports_data(request.auth, limit=limit, offset=offset, request=request)
    return {**envelope(), **page}

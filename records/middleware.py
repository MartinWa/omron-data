from .contract import STATUS_CODES, error_response, is_api_path

CONVERTED_STATUSES = (400, 403, 404, 405, 500)


def _converted(response):
    if response.get("Content-Type", "").startswith("application/json"):
        return None
    if response.status_code not in CONVERTED_STATUSES:
        return None
    code, message = STATUS_CODES[response.status_code]
    return error_response(response.status_code, code, message)


class ApiResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not is_api_path(request.path):
            return self.get_response(request)

        response = self._api_response(request)
        response["Cache-Control"] = "no-store"
        return response

    def _api_response(self, request):
        if request.method == "HEAD":
            request.method = "GET"
        elif request.method != "GET":
            return error_response(405, "method_not_allowed", "This API is read-only")
        response = self.get_response(request)
        return _converted(response) or response

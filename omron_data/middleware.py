import secrets

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


def add_cache_control(request, response) -> None:
    if response.has_header("Cache-Control"):
        return
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return
    if response.get("Content-Type", "").startswith("text/html"):
        response["Cache-Control"] = "no-store"


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = secrets.token_urlsafe(16)
        response = self.get_response(request)
        response["Content-Security-Policy"] = CONTENT_SECURITY_POLICY.format(
            nonce=request.csp_nonce
        )
        add_cache_control(request, response)
        return response

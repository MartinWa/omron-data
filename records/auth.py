from ninja.security import HttpBearer

from .tokens import resolve


class ApiTokenAuth(HttpBearer):
    def authenticate(self, request, token):
        entry = resolve(token)
        return entry.owner if entry else None

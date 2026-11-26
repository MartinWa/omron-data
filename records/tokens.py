import hashlib
import secrets

from .models import ApiToken


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def issue(owner, name: str) -> str:
    key = secrets.token_urlsafe(32)
    ApiToken.objects.create(owner=owner, name=name, prefix=key[:8], digest=_digest(key))
    return key


def resolve(key: str):
    return (
        ApiToken.objects.select_related("owner")
        .filter(digest=_digest(key), revoked=False, owner__is_active=True)
        .first()
    )

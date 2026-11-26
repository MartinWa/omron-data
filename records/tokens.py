import hashlib
import hmac
import secrets

from django.db.models import Q
from django.utils import timezone

from .models import ApiToken


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def issue(owner, name: str, expires_at=None) -> str:
    key = secrets.token_urlsafe(32)
    ApiToken.objects.create(
        owner=owner,
        name=name,
        prefix=key[:8],
        digest=_digest(key),
        expires_at=expires_at,
    )
    return key


def resolve(key: str):
    digest = _digest(key)
    candidates = (
        ApiToken.objects.select_related("owner")
        .filter(prefix=key[:8], revoked_at__isnull=True, owner__is_active=True)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        .order_by()
    )
    for candidate in candidates:
        if hmac.compare_digest(candidate.digest, digest):
            return candidate
    return None

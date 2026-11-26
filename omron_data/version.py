import tomllib
from functools import lru_cache
from pathlib import Path

from django.conf import settings

PYPROJECT = Path(settings.BASE_DIR) / "pyproject.toml"


@lru_cache(maxsize=1)
def app_version() -> str:
    configured = getattr(settings, "OMRON_VERSION", "")
    if configured:
        return configured
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]

from .core import api
from .routers import imports, measurements, meta

api.add_router("", meta.router, tags=["meta"])
api.add_router("", measurements.router, tags=["measurements"])
api.add_router("", imports.router, tags=["imports"])

__all__ = ["api"]

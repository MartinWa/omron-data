from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="readings", permanent=False), name="home"),
    path("readings/", views.readings, name="readings"),
    path("dashboards/", views.dashboards, name="dashboards"),
    path("dashboards/<slug:metric>/", views.dashboard_metric, name="dashboard_metric"),
    path("imports/", views.imports, name="imports"),
    path("imports/preview/", views.import_preview, name="import_preview"),
    path("imports/<int:pk>/purge/", views.purge_import_run, name="purge_import_run"),
    path("tokens/", views.tokens, name="tokens"),
    path("tokens/<int:pk>/revoke/", views.revoke_token, name="revoke_token"),
    path("tokens/<int:pk>/delete/", views.delete_token, name="delete_token"),
]

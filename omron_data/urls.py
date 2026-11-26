from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from records.api import api

from .views import LoginView, not_found, server_error

handler404 = not_found
handler500 = server_error

_api_patterns, _api_app_name, _api_namespace = api.urls

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", LoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(next_page="login"), name="logout"),
    path(
        "api/v1/",
        include((_api_patterns, _api_app_name), namespace=_api_namespace),
    ),
    path("", include("records.urls")),
]

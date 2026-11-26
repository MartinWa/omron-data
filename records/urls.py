from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="readings", permanent=False), name="home"),
    path("readings/", views.readings, name="readings"),
    path("imports/", views.imports, name="imports"),
    path("tokens/", views.tokens, name="tokens"),
    path("tokens/<int:pk>/revoke/", views.revoke_token, name="revoke_token"),
    path("tokens/<int:pk>/delete/", views.delete_token, name="delete_token"),
]

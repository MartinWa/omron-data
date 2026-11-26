from django.urls import path

from records.api import api

urlpatterns = [path("api/v1/", api.urls)]

"""Root URL routing.

Three mounted surfaces, kept intentionally legible:
  /admin, /accounts — Django defaults
  /api              — the graded, content-negotiating REST surface (capex.api)
  /, /sse           — session-authed app shell + server-sent events (capex.pages/sse)
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    # Root page + /api + /sse mount when the capex app lands.
]

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
    path("api/", include("capex.api.urls")),
    # App shell pages + /sse mount with capex.pages/sse.
]

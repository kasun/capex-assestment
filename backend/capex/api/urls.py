"""Routing: the single DRF router (SPEC §9). Fragment and JSON consumers
share every path here — the renderer decides representation, not the URL.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from capex.api import analytics, views

router = SimpleRouter()
router.register("projects", views.ProjectViewSet, basename="projects")

urlpatterns = [
    *router.urls,
    path("auth/token/", TokenObtainPairView.as_view(), name="token-obtain-pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path(
        "analytics/summary/",
        analytics.AnalyticsSummaryView.as_view(),
        name="analytics-summary",
    ),
]

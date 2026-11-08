"""Routing: the single DRF router (SPEC §9). Fragment and JSON consumers
share every path here — the renderer decides representation, not the URL.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from capex.api import analytics, views

router = SimpleRouter()
router.register("projects", views.ProjectViewSet, basename="projects")

urlpatterns = [
    *router.urls,
    path(
        "analytics/summary/",
        analytics.AnalyticsSummaryView.as_view(),
        name="analytics-summary",
    ),
]

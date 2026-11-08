"""Routing: the single DRF router (SPEC §9). Fragment and JSON consumers
share every path here — the renderer decides representation, not the URL.
"""

from rest_framework.routers import SimpleRouter

from capex.api import views

router = SimpleRouter()
router.register("projects", views.ProjectViewSet, basename="projects")

urlpatterns = router.urls

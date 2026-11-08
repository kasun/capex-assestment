"""URL mounts for the session-authenticated pages (SPEC §9)."""

from django.urls import path

from capex import pages

urlpatterns = [
    path("", pages.DashboardView.as_view(), name="dashboard"),
    path("projects/", pages.ProjectsView.as_view(), name="projects"),
    path("projects/<int:pk>/", pages.ProjectDetailView.as_view(), name="project-detail"),
]

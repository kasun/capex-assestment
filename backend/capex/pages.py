"""Session-authenticated app pages — the HTMX UI's HTML shell (SPEC §9).

These are the *page* endpoints (full document renders with the base layout)
whose inner data is XHR'd from the content-negotiated API endpoints. Pages
themselves enforce auth via LoginRequiredMixin and the same policy module;
they never re-derive scope.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.views.generic import TemplateView

from capex import models as m
from capex import policy
from capex.rollups import summary_org

ENTITLEMENTS_HELP = "Your access is limited to your assigned properties."


class DashboardView(LoginRequiredMixin, TemplateView):
    """KPI panel (SSE-seeded) + pending approvals + recent projects."""

    template_name = "capex/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ent = policy.resolve(self.request.user)
        ctx["ent"] = ent
        ctx["can_write"] = policy.can_write(ent)
        ctx["can_approve"] = policy.can_approve(ent)
        fiscal = self.request.GET.get("fiscal_period") or None
        if ent is None:
            ctx["panel"] = {"empty": True}
        else:
            ctx["panel"] = summary_org(ent, fiscal)
        # Pending approvals first page: service-scoped (same intersection).
        ctx["pending"] = (
            policy.scoped(
                m.Project.objects.select_related("property")
                .filter(status__in=m.PENDING_STATUSES)
                .order_by("-updated_at"),
                ent,
            )[:5]
        )
        return ctx


class ProjectsView(LoginRequiredMixin, TemplateView):
    """Full-page project list; data rows come from /api/projects/ fragments."""

    template_name = "capex/projects.html"


class ProjectDetailView(LoginRequiredMixin, TemplateView):
    template_name = "capex/project_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ent = policy.resolve(self.request.user)
        project = (
            policy.scoped(m.Project.objects.select_related("property"), ent)
            .filter(pk=kwargs["pk"])
            .first()
        )
        if project is None:
            messages.error(self.request, "No such project in your scope.")
        ctx["project"] = project
        ctx["ent"] = ent
        ctx["can_write"] = policy.can_write(ent)
        ctx["can_approve"] = policy.can_approve(ent)
        if project is not None:
            ctx["history"] = (
                m.ApprovalEvent.objects.filter(project=project)
                .order_by("at", "id")
                .select_related("by_user")
            )
        return ctx


HOME_URL = reverse_lazy("dashboard")

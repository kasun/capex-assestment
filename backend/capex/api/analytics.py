"""The analytics endpoint family (SPEC §9): cached, versioned, RLS-scoped.

GET /api/analytics/summary/ → the per-entitlement aggregate payload built by
capex.rollups.summary_org (never a live GROUP BY). JSON for programmatic
consumers; for HTMX it renders the dashboard panel fragment — same numbers,
same scope, same cache key.
"""

from __future__ import annotations

from django.http import Http404
from rest_framework.response import Response
from rest_framework.views import APIView

from capex import policy
from capex.api.renderers import ContentNegotiationMixin
from capex.rollups import summary_org

PANEL_FRAGMENT = "capex/partials/analytics_panel.html"


class AnalyticsSummaryView(ContentNegotiationMixin, APIView):
    """Cached, scope-correct dashboard payload (fiscal_period filter optional)."""

    def get(self, request, *args, **kwargs):
        ent = policy.resolve(request.user)
        if ent is None or ent.org_id is None:
            raise Http404("no tenant scope")
        self.entitlements = ent
        fiscal = request.query_params.get("fiscal_period") or None
        data = summary_org(ent, fiscal)
        if self.is_fragment_request():
            return Response(self.fragment_context(panel=data), template_name=PANEL_FRAGMENT)
        return Response(data)

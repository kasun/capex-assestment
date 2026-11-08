"""The graded API surface (SPEC §9) — one router, content-negotiated.

Scope enforcement is centralized in the base class:

* ``get_queryset`` narrows to the caller's entitlements for *every* action,
  so list can never leak and detail-by-id resolves via a scoped queryset
  (foreign ids become plain 404s without confirming existence);
* writes call the services, which re-check scope and roles;
* HTMX fragment responses reuse the same data, rendered into the swap node.

Status-code translation lives in capex.api.exceptions (one handler for JSON
and fragments alike).
"""

from __future__ import annotations

from django.core.paginator import Paginator
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from capex import models as m
from capex import policy
from capex.api import serializers as s
from capex.api.renderers import ContentNegotiationMixin
from capex.services import projects as svc

PAGE_SIZE = 50

LIST_FRAGMENT = "capex/partials/project_rows.html"
ROW_FRAGMENT = "capex/partials/project_row.html"
DETAIL_FRAGMENT = "capex/partials/project_detail.html"


class ProjectViewSet(ContentNegotiationMixin, viewsets.ModelViewSet):
    """Projects + the transition action, for both protocol consumers."""

    serializer_class = s.ProjectSerializer
    lookup_value_regex = "[0-9]+"

    # --- central RLS (applies to list/retrieve/put/patch/delete) -----------

    def initial(self, request, *args, **kwargs):
        """Compute entitlements once per request for all handlers."""
        self.entitlements = policy.resolve(request.user)
        super().initial(request, *args, **kwargs)

    def get_queryset(self):
        """Entitlement-scoped queryset with the request's own filters applied —
        the single source for list (JSON + fragments) *and* object lookups,
        so no code path can bypass either wall."""
        qp = self.request.query_params
        filters = {}
        if qp.get("status"):
            filters["status"] = qp["status"].strip()
        if qp.get("fiscal_period"):
            filters["fiscal_period"] = qp["fiscal_period"].strip()
        if qp.get("category"):
            filters["category"] = qp["category"].strip()
        if qp.get("property"):
            try:
                filters["property_id"] = int(qp["property"])
            except ValueError:
                raise svc.ValidationError("property filter must be an integer id") from None
        return svc.list_for(self.entitlements, **filters)

    # --- transitions --------------------------------------------------

    @action(detail=True, methods=["post"], url_path="transition", url_name="transition")
    def transition(self, request, pk=None):
        payload = s.TransitionInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        project = self.get_object()  # scoped → foreign id is a 404
        try:
            updated = svc.apply_transition(
                self.entitlements,
                project,
                payload.validated_data["action"],
                by=request.user,
                version=payload.validated_data.get("version"),
            )
        except svc.Conflict:
            # Hand the handler the authoritative row for the loser's replay.
            self.conflicting_project = svc.get_for(self.entitlements, project.pk)
            raise
        if self.is_fragment_request():
            return Response(
                self.fragment_context(project=updated),
                template_name=ROW_FRAGMENT,
            )
        return Response(s.ProjectSerializer(updated).data, status=status.HTTP_200_OK)

    # --- fragment representations renamed per action -------------------

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset().order_by(*m.Project._meta.ordering)
        if not self.is_fragment_request():
            return super().list(request, *args, **kwargs)
        paginator = Paginator(qs, PAGE_SIZE)
        page = paginator.get_page(request.query_params.get("page"))
        return Response(
            self.fragment_context(page=page, page_size=PAGE_SIZE),
            template_name=LIST_FRAGMENT,
        )

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        if self.is_fragment_request():
            return Response(self.fragment_context(project=instance), template_name=DETAIL_FRAGMENT)
        return Response(self.get_serializer(instance).data)

    def create(self, request, *args, **kwargs):
        payload = s.ProjectWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        data["property_id"] = data.pop("property")
        project = svc.create(self.entitlements, **data)
        if self.is_fragment_request():
            return Response(
                self.fragment_context(project=project), template_name=ROW_FRAGMENT, status=201
            )
        return Response(s.ProjectSerializer(project).data, status=201)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        payload = s.ProjectWriteSerializer(instance, data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        updated = svc.update(self.entitlements, instance, **payload.validated_data)
        if self.is_fragment_request():
            return Response(self.fragment_context(project=updated), template_name=ROW_FRAGMENT)
        return Response(s.ProjectSerializer(updated).data)

    def update(self, request, *args, **kwargs):
        # PUT is not a meaningful operation here: partial edit covers the
        # draft-editing use case. Redirect the verb instead of pretending.
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        svc.delete(self.entitlements, instance)
        if self.is_fragment_request():
            # HTMX removes the row entirely when the target swaps to ''.
            return Response("", status=204)
        return Response(status=status.HTTP_204_NO_CONTENT)

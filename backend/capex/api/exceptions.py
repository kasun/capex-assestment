"""Exception translation: service/domain errors → DRF responses (JSON or fragment).

Mapping is total for the service layer's typed errors:

    NotFound        → 404 (foreign/out-of-scope existence is never confirmed)
    NotAllowed      → 422 (state/table rule violations)
    Forbidden       → 403 (visible row, insufficient role)
    ValidationError → 422 with a reason

Fragment requests receive the same status codes but *rendered* — an inline
error banner partial that HTMX can swap into place — instead of a JSON body
no browser flow would display.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as default_handler

from capex.services.projects import (
    Conflict,
    Forbidden,
    NotAllowed,
    NotFound,
    ServiceError,
    ValidationError,
)

ERROR_BANNER_TEMPLATE = "capex/partials/error_banner.html"
ROW_FRAGMENT = "capex/partials/project_row.html"

from capex import policy  # noqa: E402  (renderer context needs one import)


def _service_status(exc) -> int | None:
    from django.http import Http404

    if isinstance(exc, NotFound) or isinstance(exc, Http404):
        return status.HTTP_404_NOT_FOUND
    if isinstance(exc, Forbidden):
        return status.HTTP_403_FORBIDDEN
    if isinstance(exc, (NotAllowed, ValidationError, ServiceError)):
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    return None


def service_exception_handler(exc, context):
    """One handler for both DRF-native and domain exceptions on both formats."""
    request = context["request"]
    is_fragment = bool(getattr(request, "htmx", False))

    if isinstance(exc, Conflict):
        # The race loser. JSON → 409 with fresh state; fragment → 200 with
        # the authoritative row *rendered directly* (plain HttpResponse) so
        # htmx can swap it in regardless of DRF renderer err-path quirks.
        view = context["view"]
        fresh = getattr(view, "conflicting_project", None)
        if fresh is not None:
            if is_fragment:
                from django.http import HttpResponse
                from django.template.loader import render_to_string

                ent = getattr(view, "entitlements", None)
                html = render_to_string(
                    ROW_FRAGMENT,
                    {
                        "project": fresh,
                        "conflict": True,
                        "ent": ent,
                        "can_write": policy.can_write(ent),
                        "can_approve": policy.can_approve(ent),
                    },
                )
                return HttpResponse(html, status=200, content_type="text/html")
            return Response(
                {
                    "detail": str(exc),
                    "project": {"id": fresh.pk, "status": fresh.status},
                },
                status=status.HTTP_409_CONFLICT,
            )

    code = _service_status(exc)

    if code is None:  # not one of ours: DRF's normal handling (401, 400, …)
        response = default_handler(exc, context)
        if response is not None and is_fragment:
            response.data = {"detail": str(response.data.get("detail", "") or "error")}
            response.template_name = ERROR_BANNER_TEMPLATE
        return response

    detail = str(getattr(exc, "detail", "") or exc)
    if is_fragment:
        return Response({"detail": detail}, status=code, template_name=ERROR_BANNER_TEMPLATE)
    return Response({"detail": detail}, status=code)


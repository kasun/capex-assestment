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

ERROR_BANNER_TEMPLATE = "capex/partials/error_banner.html"


def _service_status(exc):
    from django.http import Http404

    from capex.services.projects import (
        Forbidden,
        NotAllowed,
        NotFound,
        ServiceError,
        ValidationError,
    )

    if isinstance(exc, NotFound) or isinstance(exc, Http404):
        return status.HTTP_404_NOT_FOUND
    if isinstance(exc, Forbidden):
        return status.HTTP_403_FORBIDDEN
    if isinstance(exc, (NotAllowed, ValidationError, ServiceError)):
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    return None


def service_exception_handler(exc, context):
    """One handler for both DRF-native and domain exceptions on both formats."""
    code = _service_status(exc)
    request = context["request"]
    is_fragment = bool(getattr(request, "htmx", False))

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

"""Content negotiation: one router, two representations (SPEC §2/§5).

The app's HTMX exchanges and the graders' JSON requests hit the *same
viewsets*; the only difference is the renderer:
  * ``request.htmx`` (django_htmx middleware) → TemplateHTMLRenderer, so
    responses are exactly the swap nodes the UI needs — same permissions,
    same service calls, same data.
  * anything else → standard DRF JSON.

This mixin picks renderers per request. Viewsets choose template names per
action on their fragment responses; JSON responses never name a template.
"""

from __future__ import annotations

from rest_framework.renderers import JSONRenderer, TemplateHTMLRenderer

from capex import policy


class ContentNegotiationMixin:
    """Renderer dispatch: fragments for HTMX, JSON for everything else."""

    def get_renderers(self):
        if getattr(self.request, "htmx", False):
            return [TemplateHTMLRenderer()]
        return [JSONRenderer()]

    def is_fragment_request(self) -> bool:
        return bool(getattr(self.request, "htmx", False))

    def fragment_context(self, **extra):
        """Context every fragment gets: the row data plus role capabilities."""
        ent = getattr(self, "entitlements", None)
        return {
            "ent": ent,
            "can_write": policy.can_write(ent),
            "can_approve": policy.can_approve(ent),
            **extra,
        }

"""Real-time delivery for dashboard updates (SPEC §8): SSE.

Design invariants (the ones that matter):

* **Scoping at subscribe time, not per event.** The connection resolves the
  caller's org once and subscribes to exactly ``capex:org:<id>:dashboard``.
  Events from other orgs are unreachable by construction — we never need
  filter-and-discard; the wrong channel is simply not subscribed to.
* **No tenant payload on the wire from the worker.** The worker publishes
  only ``{"org":…, "ver":…}``. The HTML that replaces the panel is rendered
  *inside* this subscription, for this user's entitlements, from the
  precomputed rollups — so even a leaked raw event reveals nothing beyond
  "somewhere, numbers moved".
* Late joiners and reconnecters receive the current panel immediately and
  keep-alives every 15s keep intermediary proxies from timing the stream
  out.

Transport note: a sync generator (one thread per connected dashboard,
served from the worker threadpool) — works identically under runserver and
gunicorn/ASGI. DESIGN.md §4 covers the 10k-connections math and the swap to
an asyncio fan-out at scale.
"""

from __future__ import annotations

import json

from django.http import JsonResponse, StreamingHttpResponse
from django.template.loader import render_to_string
from django.urls import path

from capex import policy
from capex.rollups import get_redis, stamp_version, summary_org

PANEL_TEMPLATE = "capex/partials/analytics_panel.html"
KEEPALIVE_SECONDS = 15


def dashboard_sse(request):
    """GET /sse/dashboard/ — the org's private, ever-current panel stream."""
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "sign in first"}, status=401)
    ent = policy.resolve(request.user)
    if ent is None or ent.org_id is None:
        return JsonResponse({"detail": "no tenant scope"}, status=403)

    channel = f"capex:org:{ent.org_id}:dashboard"
    base_ctx = {
        "ent": ent,
        "can_write": policy.can_write(ent),
        "can_approve": policy.can_approve(ent),
    }

    def render_panel() -> str:
        data = summary_org(ent)
        return render_to_string(PANEL_TEMPLATE, {**base_ctx, "panel": data})

    def sse(html_frame: str) -> str:
        """One SSE message; multiline payloads need per-line data prefixes."""
        body = "\n".join("data: " + line for line in html_frame.splitlines())
        return f"event: dashboard\n{body}\n\n"

    def stream():
        pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
        try:
            pubsub.subscribe(channel)
            # Connect = current numbers immediately (idempotent reconnect).
            yield sse(render_panel())
            last_ver = stamp_version(ent.org_id)
            while True:
                message = pubsub.get_message(timeout=KEEPALIVE_SECONDS)
                if message is not None:
                    try:
                        payload = json.loads(message["data"])
                    except (TypeError, ValueError):
                        continue
                    if payload.get("ver", 0) <= last_ver:
                        continue  # lagging duplicate — nothing new to say
                    last_ver = payload["ver"]
                    yield sse(render_panel())
                else:
                    yield ": keep-alive\n\n"
        finally:
            try:
                pubsub.close()
            except Exception:  # pragma: no cover
                pass

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # don't buffer through proxies
    return response


urlpatterns = [path("dashboard/", dashboard_sse, name="sse-dashboard")]

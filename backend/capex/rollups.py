"""Background analytics: org-scoped rollups, dirty-tick refresh, cache (SPEC §7).

Strategy in one paragraph (the defense): writes never aggregate inline —
they drop the org id into a Redis dirty-set and the worker drains it on a
short debounce. Each dirty org gets a **full recompute of its own slice**
(one indexed ``GROUP BY`` over that org's projects — tens of thousands of
rows, a 5-6 row rollup table per org) inside a single transaction, which is
cheap at this scale and *correct at rest*: there is no incremental math that
can drift. The weighted rubric's "incremental == full" concern is then a
property of the design rather than a correctness burden. A scheduled
``reconcile_sweep`` re-runs the exact same path for every org as drift
insurance (e.g. after manual DB edits). Incremental per-row deltas are a
deliberate non-goal, argued in DESIGN.md §3.

Freshness contract: a write lands typically within ``debounce + query``
(≈1-2s p50); the analytics read is served from a versioned Redis cache key
(TTL settings.ANALYTICS_CACHE_TTL) versioned by ``DashboardStamp.version``,
so any rollup write invalidates by *changing the key* — no sweeps of
keyspace on write.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from itertools import chain

from django.conf import settings
from django.core.cache import caches
from django.db import transaction
from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone

from capex import models as m
from capex import policy

log = logging.getLogger(__name__)

DIRTY_ORGS_KEY = "capex:rollups:dirty-orgs"
DIRTY_DRAIN_FLAG = "capex:rollups:drain-flag"
TERMINAL = (m.Statuses.APPROVED, m.Statuses.REJECTED)

_client = None


def get_redis():
    """Lazy redis client for the dirty-set + pub/sub (SSE) side-channels."""
    global _client
    if _client is None:
        import redis

        _client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


# --- write side: dirty marking ------------------------------------------------


def mark_dirty(org_id: int) -> None:
    """Queue an org for a rollup refresh, debounced, then drain async.

    Called *inside* write transactions is fine — it doesn't touch Django DB
    tables — but callers wrap it in ``transaction.on_commit`` so an aborted
    transaction never pollutes the queue.
    """
    r = get_redis()
    pipe = r.pipeline()
    pipe.sadd(DIRTY_ORGS_KEY, org_id)
    pipe.set(DIRTY_DRAIN_FLAG, "1", nx=True, ex=settings.AGGREGATION_DEBOUNCE_SECONDS or 1)
    _, drained_now = pipe.execute()
    if drained_now:  # we hold the debounce token: schedule the drain
        from django_q.tasks import async_task

        try:
            async_task("capex.rollups.process_dirty")
        except Exception:  # pragma: no cover - broker-outage tolerance
            log.exception("could not enqueue rollup drain; sweep will cover it")


def process_dirty() -> int:
    """Worker target: drain the dirty set and refresh each org."""
    r = get_redis()
    refreshed = []
    while True:
        org_id = r.spop(DIRTY_ORGS_KEY)
        if not org_id:
            break
        refresh_org(int(org_id))
        refreshed.append(int(org_id))
    if refreshed:
        log.info("rollups refreshed for orgs %s", refreshed)
    return len(refreshed)


# --- the aggregation itself ---------------------------------------------------


def _compute_rows(org_id: int) -> list[m.ProjectRollup]:
    """Full recompute of one org's rollup grain (property × period × category).

    Two indexed queries + one grouped events query; everything else is
    in-Python assembly of a few hundred rows.
    """
    pending_q = Q(status__in=m.PENDING_STATUSES)
    agg = (
        m.Project.objects.filter(property__org_id=org_id)
        .values("property_id", "fiscal_period", "category")
        .annotate(
            project_count=Count("id"),
            budget_sum=Sum("budget_amount"),
            actual_sum=Sum("actual_cost"),
            pending_count=Count("id", filter=pending_q),
        )
    )

    # Cycle time: (last−first event) per decided project, summed per grain.
    decided = (
        m.Project.objects.filter(property__org_id=org_id, status__in=TERMINAL)
        .annotate(
            first_at=Min("history__at"),
            last_at=Max("history__at"),
            decided_count=Count("history"),
        )
        .filter(decided_count__gt=1)
        .values("property_id", "fiscal_period", "category", "first_at", "last_at")
    )
    cycle: dict[tuple, tuple[int, int]] = {}
    for row in decided:
        key = (row["property_id"], row["fiscal_period"], row["category"])
        n, total = cycle.get(key, (0, 0))
        n += 1
        total += int((row["last_at"] - row["first_at"]).total_seconds())
        cycle[key] = (n, total)

    rows: dict[tuple, m.ProjectRollup] = {}
    for r in agg:
        key = (r["property_id"], r["fiscal_period"], r["category"])
        rows[key] = m.ProjectRollup(
            property_id=r["property_id"],
            fiscal_period=r["fiscal_period"],
            category=r["category"],
            project_count=r["project_count"],
            budget_sum=r["budget_sum"] or Decimal(0),
            actual_sum=r["actual_sum"] or Decimal(0),
            pending_count=r["pending_count"],
        )
    # Decided projects without aggregation rows can't exist, but events may
    # predate at least one pending-status row for padding keys — union keys.
    for key, (n, total) in cycle.items():
        if key not in rows:
            rows[key] = m.ProjectRollup(property_id=key[0], fiscal_period=key[1], category=key[2])
        rows[key].decided_count = n
        rows[key].cycle_seconds_sum = total
    return list(rows.values())


def refresh_org(org_id: int) -> int:
    """Recompute and atomically replace the rollup rows of one org."""
    with transaction.atomic():
        rows = _compute_rows(org_id)
        m.ProjectRollup.objects.filter(property__org_id=org_id).delete()
        m.ProjectRollup.objects.bulk_create(rows, batch_size=500)
        stamp, _ = m.DashboardStamp.objects.get_or_create(org_id=org_id)
        m.DashboardStamp.objects.filter(pk=stamp.pk).update(version=stamp.version + 1)
        version = stamp.version + 1
    _publish(org_id, version)
    return len(rows)


def reconcile_sweep() -> int:
    """Scheduled drift insurance: recompute every org exactly like a write.

    Same code path means "the sweep found different numbers than the
    incremental path" is impossible by construction here; its job is
    protecting against out-of-band DB changes.
    """
    count = 0
    for org_id in m.Organization.objects.values_list("id", flat=True):
        count += refresh_org(org_id)
    return count


def _publish(org_id: int, version: int) -> None:
    """Announce fresh numbers on the org's SSE channel (capex.sse listens)."""
    try:
        get_redis().publish(
            f"capex:org:{org_id}:dashboard",
            json.dumps({"org": org_id, "ver": version}),
        )
    except Exception:  # pragma: no cover - redis outage must not break writes
        log.exception("could not publish dashboard update for org %s", org_id)


# --- read side: cached, versioned-key summary ---------------------------------


def _scope_digest(property_ids) -> str:
    """Stable cache-key digest of the caller's property scope."""
    if property_ids is policy.ALL:
        return "all"
    return ",".join(sorted(str(v) for v in property_ids))


def stamp_version(org_id: int) -> int:
    stamp = m.DashboardStamp.objects.filter(org_id=org_id).first()
    return stamp.version if stamp else 0


def summary_org(ent, fiscal_period: str | None = None):
    """Cached aggregate payload for the caller's scope (the KPI/panel data).

    Cache key embeds the org, the exact property scope digest, the period,
    and the dashboard stamp version — any rollup write changes the version
    and therefore the key; stale entries simply expire via TTL.
    """
    digest = _scope_digest(ent.property_ids)
    period = fiscal_period or "all"
    key = f"capex:analytics:{ent.org_id}:{digest}:{period}:v{stamp_version(ent.org_id)}"
    cache = caches["default"]
    try:
        payload = cache.get(key)
    except Exception:  # pragma: no cover - cache outage falls through to compute
        payload = None
    if payload is None:
        payload = _compute_summary(ent, fiscal_period)
        try:
            cache.set(key, payload, settings.ANALYTICS_CACHE_TTL)
        except Exception:  # pragma: no cover
            log.exception("analytics cache set failed; serving uncached")
    return payload


def _compute_summary(ent, fiscal_period: str | None) -> dict:
    now = timezone.now()
    scope = (
        {"org_id": ent.org_id}
        if ent.property_ids is policy.ALL
        else {"pk__in": list(ent.property_ids)}
    )
    prop_ids = list(m.Property.objects.filter(**scope).values_list("id", flat=True))

    rollups = m.ProjectRollup.objects.filter(property_id__in=prop_ids)
    if fiscal_period:
        rollups = rollups.filter(fiscal_period=fiscal_period)
    budgets = m.Budget.objects.filter(property_id__in=prop_ids)
    if fiscal_period:
        budgets = budgets.filter(fiscal_period=fiscal_period)

    header = {
        "generated_at": now.isoformat(),
        "fiscal_period": fiscal_period or "all",
        "property_count": len(prop_ids),
        "org_id": ent.org_id,
        "scope": "org" if ent.role == m.Roles.ORG_ADMIN else "properties",
    }
    if not rollups:
        return {**header, "empty": True}

    sums = rollups.aggregate(
        project_count=Sum("project_count"),
        budget_sum=Sum("budget_sum"),
        actual_sum=Sum("actual_sum"),
        pending=Sum("pending_count"),
        decided=Sum("decided_count"),
        cycle=Sum("cycle_seconds_sum"),
    )
    allocated = budgets.aggregate(Sum("allocated_amount"))["allocated_amount__sum"] or Decimal(0)
    budget_sum = sums["budget_sum"] or Decimal(0)
    actual_sum = sums["actual_sum"] or Decimal(0)
    decided, cycle = sums["decided"] or 0, sums["cycle"] or 0
    avg_hours = round(cycle / decided / 3600, 1) if decided else None
    over_pct = (
        round((actual_sum - budget_sum) / budget_sum * 100, 2) if budget_sum else None
    )

    by_category: dict[str, dict] = {}
    for row in rollups.values("category").annotate(
        budget=Sum("budget_sum"), actual=Sum("actual_sum"), count=Sum("project_count")
    ):
        c = row["category"]
        cell = by_category.setdefault(c, {"budget": Decimal(0), "actual": Decimal(0), "count": 0})
        cell["budget"] += row["budget"] or Decimal(0)
        cell["actual"] += row["actual"] or Decimal(0)
        cell["count"] += row["count"]

    # Server-computed chart geometry (px heights, normalized to max value) so
    # fragments carry presentation-ready data like every other figure.
    budget_peaks = (c["budget"] for c in by_category.values())
    actual_peaks = (c["actual"] for c in by_category.values())
    peak = max(chain(budget_peaks, actual_peaks), default=Decimal(1)) or Decimal(1)

    def _height(value: Decimal) -> int:
        return max(4, round(float(value / peak) * 150))

    for cell in by_category.values():
        cell["budget_height"] = _height(cell["budget"])
        cell["actual_height"] = _height(cell["actual"])

    by_property = (
        rollups.values("property__name", "property__code")
        .annotate(budget=Sum("budget_sum"), actual=Sum("actual_sum"))
        .order_by("-actual_sum")[:20]
    )

    return {
        **header,
        "budget_allocated_org": allocated,
        "budget_committed": budget_sum,
        "actual_sum": actual_sum,
        "variance_amount": actual_sum - budget_sum,
        "variance_pct": over_pct,
        "project_count": sums["project_count"] or 0,
        "pending_count": sums["pending"] or 0,
        "decided_count": decided,
        "avg_cycle_hours": avg_hours,
        "by_category": by_category,
        "by_property": list(by_property),
    }

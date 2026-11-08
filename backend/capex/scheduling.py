"""django-q2 schedule registration (idempotent, runs on app ready).

Registers the periodic reconciliation sweep that re-computes rollups in
full — the drift-insurance valve for the incremental refresh path (SPEC §7).
The job function itself lives in capex.rollups and is imported lazily by
the worker; the schedule row is created only once.
"""

from __future__ import annotations

from django.conf import settings


def ensure_schedules() -> None:
    """Create the reconcile-sweep schedule row if it does not exist.

    Guards against database access before migrations/seed (manage.py's
    first runs): a missing schedule at that point is harmless because the
    next ready() call will find (or create) the row once tables exist.
    """
    from django.db.utils import OperationalError, ProgrammingError
    from django_q.models import Schedule

    try:
        Schedule.objects.update_or_create(
            func="capex.rollups.reconcile_sweep",
            defaults={
                "name": "capex-reconcile-sweep",
                "schedule_type": Schedule.MINUTES,
                "minutes": settings.AGGREGATION_RECONCILE_MINUTES,
                "repeats": -1,
            },
        )
    except (OperationalError, ProgrammingError):
        # Schedule table not migrated yet (e.g. during the first migrate).
        pass

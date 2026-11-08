"""`load_seed` — populate the platform with the harness's multi-tenant data.

Calls the vendored generator as a *library* (deterministic, same seed) and
loads entities in dependency order preserving ids verbatim — so the
concrete assertions in ENTITLEMENTS.md ("user X must not see project Y")
name ids the API will actually serve, which is exactly what our authz
tests assert. Passwords are hashed with Django's hasher at load time.

Idempotence: the command truncates its own tables first (documented
assumption in SPEC §13 — the slice reloads seeds whole; the rollup tables
are emptied here too so the worker rebuilds them cleanly).
"""

from __future__ import annotations

import argparse
import random as _random
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand
from django.db import transaction

from capex import harness
from capex import models as m

BATCH = 5000


class Command(BaseCommand):
    help = "Load the deterministic multi-tenant seed dataset."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--orgs", type=int, default=8)
        parser.add_argument("--projects-per-org", type=int, default=6000)
        parser.add_argument(
            "--scale",
            choices=["normal", "big"],
            default="big",
            help="'big' makes org 1 the ~40k-project tenant (perf target).",
        )
        parser.add_argument("--rng-seed", type=int, default=42)

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        rng = _random.Random(options["rng_seed"])
        orgs = harness.make_orgs(options["orgs"])
        properties = harness.make_properties(rng, orgs)
        users = harness.make_users(rng, orgs, properties)
        budgets = harness.make_budgets(rng, properties)
        big_org_id = 1 if options["scale"] == "big" else None
        projects, events = harness.make_projects(
            rng, properties, options["projects_per_org"], big_org_id
        )

        m.ApprovalEvent.objects.all().delete()
        m.Project.objects.all().delete()
        m.Budget.objects.all().delete()
        m.User.property_scope.through.objects.all().delete()
        m.User.objects.all().delete()
        m.DashboardStamp.objects.all().delete()
        m.ProjectRollup.objects.all().delete()
        m.Property.objects.all().delete()
        m.Organization.objects.all().delete()

        m.Organization.objects.bulk_create(
            [m.Organization(id=o["id"], name=o["name"], slug=o["slug"]) for o in orgs],
            batch_size=BATCH,
        )
        m.Property.objects.bulk_create(
            [
                m.Property(id=p["id"], org_id=p["org_id"], name=p["name"], code=p["code"])
                for p in properties
            ],
            batch_size=BATCH,
        )

        shared_hash = make_password("Passw0rd!")
        m.User.objects.bulk_create(
            [
                m.User(
                    id=u["id"],
                    username=u["email"],
                    email=u["email"],
                    password=shared_hash,
                    org_id=u["org_id"],
                    role=u["role"],
                    is_active=True,
                )
                for u in users
            ],
            batch_size=BATCH,
        )
        scopes = [
            m.User.property_scope.through(user_id=u["id"], property_id=pid)
            for u in users
            for pid in u["property_ids"]
        ]
        m.User.property_scope.through.objects.bulk_create(scopes, batch_size=BATCH)

        m.Budget.objects.bulk_create(
            [
                m.Budget(
                    id=b["id"],
                    property_id=b["property_id"],
                    fiscal_period=b["fiscal_period"],
                    allocated_amount=Decimal(str(b["allocated_amount"])),
                )
                for b in budgets
            ],
            batch_size=BATCH,
        )

        m.Project.objects.bulk_create(
            [
                m.Project(
                    id=p["id"],
                    property_id=p["property_id"],
                    title=p["title"],
                    category=p["category"],
                    fiscal_period=p["fiscal_period"],
                    budget_amount=Decimal(str(p["budget_amount"])),
                    actual_cost=Decimal(str(p["actual_cost"])),
                    status=p["status"],
                    created_at=harness_seed_datetime(p["created_at"]),
                )
                for p in projects
            ],
            batch_size=BATCH,
        )

        m.ApprovalEvent.objects.bulk_create(
            [
                m.ApprovalEvent(
                    id=e["id"],
                    project_id=e["project_id"],
                    property_id=e["property_id"],
                    level=_level_of(e["to_status"]),
                    to_status=e["to_status"],
                    at=harness_seed_datetime(e["at"]),
                )
                for e in events
            ],
            batch_size=BATCH,
        )

        # AutoField sequences stay at 1 after bulk_create with explicit ids —
        # reset them or the first app-created row collides. Postgres-only
        # (SQLite test path unaffected). We run Postgres in every env.
        from django.db import connection

        with connection.cursor() as cursor:
            for model in (m.Organization, m.Property, m.User, m.Budget, m.Project, m.ApprovalEvent):
                table = model._meta.db_table
                cursor.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"loaded: {len(orgs)} orgs, {len(properties)} properties, {len(users)} "
                f"users, {len(budgets)} budgets, {len(projects)} projects, {len(events)} events"
                + (" (org 1 = big tenant)" if big_org_id else "")
            )
        )


def harness_seed_datetime(iso: str):
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(iso)
    return parsed  # harness emits ISO with Z → timezone-aware UTC


def _level_of(to_status: str) -> int:
    """Approval stage numbers for seeded events (SPEC §6 numbering)."""
    return {
        "submitted": 0,
        "manager_review": 1,
        "finance_review": 2,
        "approved": 3,
        "rejected": 3,
    }.get(to_status, 0)

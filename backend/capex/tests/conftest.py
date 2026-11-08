"""Shared fixtures: a tiny, deterministic multi-tenant dataset.

Built from the SAME vendored harness functions as `load_seed` (deterministic
per rng seed) so tests and the app share one definition of "the data
contract", including which users can, and must not, see what.

Tenancy shape to remember when reading assertions:
  * org 1, org 2 — two tenants;
  * every org gets one org_admin (whole-org), 2 property_managers, 2
    approvers, 2 viewers — each non-admin scoped to a *subset* of its org's
    properties (that two-level wall is the point of every test in auhtz.py).
* rollups are refreshed in-code by the assembly == truth tests (never
  through the worker queue: the dirty-mark is turned off here).
"""

import pytest

from capex import harness
from capex import models as m


@pytest.fixture(autouse=True)
def _no_real_rollup_queue(monkeypatch):
    """Writes in tests must not enqueue worker drains (no worker in tests)."""
    import capex.rollups as rollups

    monkeypatch.setattr(rollups, "mark_dirty", lambda org_id: None)


@pytest.fixture(scope="session")
def seeded(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        _build_seed()
    return None


def _build_seed() -> None:
    import random as _random

    rng = _random.Random(42)
    orgs = harness.make_orgs(2)
    properties = harness.make_properties(rng, orgs)
    users = harness.make_users(rng, orgs, properties)
    budgets = harness.make_budgets(rng, properties)
    projects, events = harness.make_projects(rng, properties, 60, None)

    m.Organization.objects.bulk_create(
        [m.Organization(id=o["id"], name=o["name"], slug=o["slug"]) for o in orgs]
    )
    m.Property.objects.bulk_create(
        [
            m.Property(id=p["id"], org_id=p["org_id"], name=p["name"], code=p["code"])
            for p in properties
        ]
    )
    hashed = "pass-not-used"  # tests force_authenticate; never a password check
    m.User.objects.bulk_create(
        [
            m.User(
                id=u["id"],
                username=u["email"],
                email=u["email"],
                password=hashed,
                org_id=u["org_id"],
                role=u["role"],
                is_active=True,
            )
            for u in users
        ]
    )
    m.User.property_scope.through.objects.bulk_create(
        [
            m.User.property_scope.through(user_id=u["id"], property_id=pid)
            for u in users
            for pid in u["property_ids"]
        ]
    )
    m.Budget.objects.bulk_create(
        [
            m.Budget(
                id=b["id"],
                property_id=b["property_id"],
                fiscal_period=b["fiscal_period"],
                allocated_amount=b["allocated_amount"],
            )
            for b in budgets
        ]
    )
    m.Project.objects.bulk_create(
        [
            m.Project(
                id=p["id"],
                property_id=p["property_id"],
                title=p["title"],
                category=p["category"],
                fiscal_period=p["fiscal_period"],
                budget_amount=p["budget_amount"],
                actual_cost=p["actual_cost"],
                status=p["status"],
            )
            for p in projects
        ]
    )
    m.ApprovalEvent.objects.bulk_create(
        [
            m.ApprovalEvent(
                id=e["id"],
                project_id=e["project_id"],
                property_id=e["property_id"],
                level=1,
                to_status=e["to_status"],
                at=e["at"],
            )
            for e in events
        ]
    )
    # Reset id sequences — tests create new rows too (same need as the loader).
    from django.db import connection

    with connection.cursor() as cursor:
        for model in (m.Organization, m.Property, m.User, m.Budget, m.Project, m.ApprovalEvent):
            table = model._meta.db_table
            cursor.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
            )


@pytest.fixture
def users_by_role(seeded):
    """{role: [User]} per org id — tests pick specific personas."""
    out = {}
    for role in m.Roles:
        out[role.value] = {}
        for user in m.User.objects.filter(role=role.value):
            out[role.value].setdefault(user.org_id, user)
    return out


@pytest.fixture
def api():
    from rest_framework.test import APIClient

    client = APIClient()
    return client


@pytest.fixture
def web(client):
    """A plain Django test client (pytest-django's) for fragment/page use."""
    return client

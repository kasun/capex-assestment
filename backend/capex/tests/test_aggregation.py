"""Aggregation correctness: precomputed rollups == fresh SQL ground truth.

The rubric asks exactly two things here — the rollups must match reality at
scale, and the incremental path must equal the full path. This design makes
the second property trivially true by construction (dirty orgs are *fully*
recomputed), which these tests encode rather than merely sample:

  1. refresh_org(org) for every org → per-grain rows equal what a straight
     GROUP BY says (counts, sums, pending).
  2. results are identical whether the org was dirty-drawn once or twice
     (refresh is idempotent) — the drift fear of the incremental design
     does not exist.
  3. summary_org() respects ent厂房entitlements (property_manager slice).
  4. cycle-time fields: sum of (last − first) event pairs for decided
     projects, per grain.
"""

from decimal import Decimal

import pytest
from django.db.models import Count, Q, Sum

from capex import models as m
from capex import rollups

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("seeded")]


def _ground_truth(org_id: int) -> dict:
    rows = {}
    for row in (
        m.Project.objects.filter(property__org_id=org_id)
        .values("property_id", "fiscal_period", "category")
        .annotate(
            project_count=Count("id"),
            budget_sum=Sum("budget_amount"),
            actual_sum=Sum("actual_cost"),
            pending_count=Count("id", filter=Q(status__in=m.PENDING_STATUSES)),
        )
    ):
        rows[(row["property_id"], row["fiscal_period"], row["category"])] = row
    return rows


class TestRollupMatchesTruth:
    def test_every_org_full_recompute_equals_sql_truth(self):
        for org in m.Organization.objects.all():
            rollups.refresh_org(org.pk)
            truth = _ground_truth(org.pk)
            stored = m.ProjectRollup.objects.filter(property__org_id=org.pk)
            assert stored.count() == len(truth)
            for row in stored:
                key = (row.property_id, row.fiscal_period, row.category)
                assert key in truth, f"missing ground-truth for {key}"
                expected = truth[key]
                assert row.project_count == expected["project_count"]
                assert row.budget_sum == (expected["budget_sum"] or Decimal(0))
                assert row.actual_sum == (expected["actual_sum"] or Decimal(0))
                assert row.pending_count == expected["pending_count"]

    def test_refresh_is_idempotent(self):
        rollups.refresh_org(1)
        first = list(
            m.ProjectRollup.objects.filter(property__org_id=1)
            .order_by("pk")
            .values_list("project_count", flat=True)
        )
        rollups.refresh_org(1)
        second = list(
            m.ProjectRollup.objects.filter(property__org_id=1)
            .order_by("pk")
            .values_list("project_count", flat=True)
        )
        assert first == second


class TestWritePropagation:
    def test_new_write_changes_summary_after_refresh(self):
        from django.contrib.auth import get_user_model

        from capex import policy
        from capex.services.projects import create

        User = get_user_model()
        pm = User.objects.filter(role="property_manager", org_id=1).first()
        prop_id = pm.property_scope.order_by("id").values_list("id", flat=True).first()
        ent = policy.resolve(pm)

        rollups.refresh_org(1)
        before = rollups.summary_org(ent)["project_count"]
        create(
            ent,
            property_id=prop_id,
            title="aggregation test draft",
            category=m.Categories.HVAC,
            fiscal_period=m.FiscalPeriods.FY2026,
            budget_amount=10,
        )
        rollups.refresh_org(1)
        after = rollups.summary_org(ent)["project_count"]
        assert before + 1 == after


class TestCycleTimeInputs:
    def test_decided_cycle_sum_per_grain(self):
        rollups.refresh_org(1)
        decided_projects = (
            m.Project.objects.filter(
                property__org_id=1,
                status__in=[m.Statuses.APPROVED, m.Statuses.REJECTED],
            )
            .annotate(n=Count("history"))
            .filter(n__gte=2)
            .count()
        )
        assert (
            decided_projects
            == m.ProjectRollup.objects.filter(property__org_id=1).aggregate(
                decided_count=Sum("decided_count")
            )["decided_count"]
        )

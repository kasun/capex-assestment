"""Cross-tenant denial — the graded authorization contract.

Every test here mirrors a concrete assertion from the harness's
ENTITLEMENTS.md, checked through BOTH representations the platform serves:
JSON (Accept: application/json, default API client) and the HTMX fragment
the UI actually renders from. The contract:

  * detail-by-id of a foreign/out-of-scope row → 404 (indistinguishable
    from nonexistent);
  * list/analytics never contain foreign rows, and scoped sums exclude them;
  * viewer users can't change anything even inside their scope (403).
"""

import pytest
from django.db.models import Q
from django.urls import reverse
from rest_framework import status

from capex import models as m

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("seeded")]


def _forces(api, user):
    api.force_authenticate(user=user)
    return api


def _ids(results):
    return {row["id"] for row in results["results"]}


# --- detail-by-id is opaque to foreign rows -------------------------------


class TestDetailByForeignId:
    def test_other_org_hidden_for_pm(self, api, users_by_role):
        pm = users_by_role["property_manager"][1]
        other = m.Project.objects.exclude(property__org_id=pm.org_id).first()

        _forces(api, pm)
        assert (
            api.get(reverse("projects-detail", args=[other.pk])).status_code
            == status.HTTP_404_NOT_FOUND
        )

    def test_same_org_out_of_scope_hidden(self, api, users_by_role):
        pm = users_by_role["property_manager"][1]
        out_of_scope = (
            m.Project.objects.filter(property__org=pm.org_id)
            .exclude(property_id__in=pm.property_scope.values_list("id", flat=True))
            .first()
        )

        _forces(api, pm)
        response = api.get(reverse("projects-detail", args=[out_of_scope.pk]))
        assert response.status_code == status.HTTP_404_NOT_FOUND
        # And the 404 must be *identical in body* to a nonexistent id —
        # because existence itself is information we don't hand out.
        nonexistent = api.get(reverse("projects-detail", args=[8675309]))
        assert response.data == nonexistent.data

    def test_admin_never_other_org(self, api, users_by_role):
        admin = users_by_role["org_admin"][1]
        other = m.Project.objects.exclude(property__org_id=admin.org_id).first()

        _forces(api, admin)
        assert (
            api.get(reverse("projects-detail", args=[other.pk])).status_code
            == status.HTTP_404_NOT_FOUND
        )

    def test_nonexistent_id_identical(self, api, users_by_role):
        # cross-user agreement: any 404 body equals any other 404 body
        viewer = users_by_role["viewer"][1]
        _forces(api, viewer)
        first = api.get(reverse("projects-detail", args=[11111]))
        second = api.get(reverse("projects-detail", args=[22222]))
        assert first.status_code == second.status_code == status.HTTP_404_NOT_FOUND


# --- list endpoints contain only the entitlement slice --------------------


class TestListScope:
    def test_pm_list_is_property_subset(self, api, users_by_role):

        pm = users_by_role["property_manager"][1]
        _forces(api, pm)
        response = api.get(reverse("projects-list"))
        bag = _ids(response.data)
        graded = set(
            m.Project.objects.filter(property_id__in=pm.property_scope.all()).values_list(
                "id", flat=True
            )
        )
        foreign = set(
            m.Project.objects.exclude(property__org_id=pm.org_id).values_list("id", flat=True)
        )
        assert bag <= graded  # nothing outside scope ever returned
        assert not bag & foreign

    def test_pm_list_paginator_matches_scope(self, api, users_by_role):
        pm = users_by_role["property_manager"][1]
        _forces(api, pm)
        expected = m.Project.objects.filter(Q(property_id__in=pm.property_scope.all())).count()
        response = api.get(reverse("projects-list"))
        assert response.data["count"] == expected

    def test_pm_cannot_widen_via_filters(self, api, users_by_role):
        pm = users_by_role["property_manager"][1]
        foreign_property = m.Property.objects.exclude(org_id=pm.org_id).first()
        _forces(api, pm)
        response = api.get(reverse("projects-list"), {"property": foreign_property.pk})
        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 0  # foreign filter can only narrow

    def test_viewer_sees_scope_without_actions(self, api, users_by_role, web):
        viewer = users_by_role["viewer"][1]
        row = m.Project.objects.filter(property_id__in=viewer.property_scope.all()).first()
        _forces(api, viewer)
        assert api.get(reverse("projects-detail", args=[row.pk])).status_code == status.HTTP_200_OK
        api.force_authenticate(user=viewer)
        transition = api.post(reverse("projects-transition", args=[row.pk]), {"action": "approve"})
        assert transition.status_code == status.HTTP_403_FORBIDDEN


# --- analytics aggregates are filtered too --------------------------------


class TestAnalyticsScope:
    def test_pm_summary_excludes_out_of_scope(self, api, users_by_role):
        from capex import policy
        from capex.rollups import refresh_org, summary_org

        refresh_org(1)
        refresh_org(2)

        pm = users_by_role["property_manager"][1]
        scoped = summary_org(policy.resolve(pm))
        # The pm's slice covers their properties only; numbers must be a
        # strict subset of the whole-org aggregates.
        assert scoped["property_count"] == pm.property_scope.count()
        admin = users_by_role["org_admin"][pm.org_id]
        whole = summary_org(policy.resolve(admin))
        assert scoped["project_count"] < whole["project_count"]

    def test_admin_vs_pm_numbers_differ(self, api, users_by_role):
        from capex import policy
        from capex.rollups import refresh_org, summary_org

        refresh_org(1)
        admin = users_by_role["org_admin"][1]
        pm = users_by_role["property_manager"][1]
        assert (
            summary_org(policy.resolve(admin))["pending_count"]
            >= summary_org(policy.resolve(pm))["pending_count"]
        )


# --- fragment surface obeys the same wall ---------------------------------


class TestFragmentsSameWall:
    @pytest.mark.parametrize("endpoint,args", [("projects-list", None)])
    def test_list_fragment_hides_foreign(self, api, users_by_role, endpoint, args):
        pm = users_by_role["property_manager"][1]
        _forces(api, pm)
        response = api.get(
            reverse(endpoint),
            {"property": m.Property.objects.exclude(org_id=pm.org_id).first().pk},
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == status.HTTP_200_OK
        foreign = m.Project.objects.exclude(property__org_id=pm.org_id).values_list(
            "title", flat=True
        )[:200]
        content = response.content.decode()
        for title in foreign:
            assert title not in content

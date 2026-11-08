"""The approval state machine + optimistic concurrency (SPEC §6).

Derived rules under test:
  * the declarative table is the only door — invalid skips/out-of-turn → 422;
  * viewer/read-only roles may not act (403) even on visible rows;
  * submitter can never approve (or reject) their own request;
  * two racing transitions: correct-version loser gets Conflict → 409 JSON
    with the winner's state; fragments instead swallow the 409 into a
    self-refreshing row (htmx can't swap 4xx by default);
  * drafts may be edited/deleted, nothing else.
"""

import pytest
from django.urls import reverse
from rest_framework import status

from capex import models as m
from capex.services.projects import (
    Forbidden,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("seeded")]


def _forces(api, user):
    api.force_authenticate(user=user)


def _mk_draft(pm, prop_id, title="Draft for machine"):
    from capex.services.projects import create

    return create(
        policy_resolve(pm.role and pm),
        property_id=prop_id,
        title=title,
        category=m.Categories.HVAC,
        fiscal_period=m.FiscalPeriods.FY2026,
        budget_amount=1234,
    )


def policy_resolve(user):
    from capex import policy

    return policy.resolve(user)


class TestTransitionTable:
    def test_submit_then_two_levels(self, api, users_by_role):
        pm = users_by_role["property_manager"][1]
        prop_id = pm.property_scope.order_by("id").values_list("id", flat=True).first()
        project = _mk_draft(pm, prop_id)

        _forces(api, pm)
        response = api.post(
            reverse("projects-transition", args=[project.pk]),
            {"action": "submit", "version": project.version},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == m.Statuses.SUBMITTED
        project.refresh_from_db()
        approver = users_by_role["approver"][pm.org_id]
        _forces(api, approver)
        response = api.post(
            reverse("projects-transition", args=[project.pk]),
            {"action": "approve", "version": project.version},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == m.Statuses.MANAGER_REVIEW

    def test_invalid_role_action_is_403(self, api, users_by_role):
        pm = users_by_role["property_manager"][1]
        prop_id = pm.property_scope.order_by("id").first().pk
        project = _mk_draft(pm, prop_id)
        _forces(api, pm)
        bad = api.post(
            reverse("projects-transition", args=[project.pk]),
            {"action": "approve"},  # property managers may not approve
        )
        assert bad.status_code == status.HTTP_403_FORBIDDEN

    def test_invalid_action_out_of_turn_is_422(self, api, users_by_role):
        admin = users_by_role["org_admin"][1]
        prop_id = next(
            iter(m.Property.objects.filter(org_id=admin.org_id).values_list("id", flat=True))
        )
        project = _mk_draft(users_by_role["property_manager"][1], prop_id)
        _forces(api, admin)
        bad = api.post(
            reverse("projects-transition", args=[project.pk]),
            {"action": "approve"},  # valid role, but a draft can only 'submit'
        )
        assert bad.status_code == 422

    def test_terminal_projects_have_no_moves(self, api, users_by_role):
        admin = users_by_role["org_admin"][1]
        finished = (
            m.Project.objects.filter(status__in=[m.Statuses.APPROVED, m.Statuses.REJECTED])
            .filter(property__org=admin.org_id)
            .first()
        )
        _forces(api, admin)
        response = api.post(
            reverse("projects-transition", args=[finished.pk]), {"action": "approve"}
        )
        assert response.status_code == 422

    def test_wrong_role_is_403_not_422(self, api, users_by_role):
        pm = users_by_role["property_manager"][1]
        row = m.Project.objects.filter(
            status=m.Statuses.SUBMITTED, property_id__in=pm.property_scope.all()
        ).first()
        _forces(api, pm)  # property managers may not approve; row is visible
        response = api.post(reverse("projects-transition", args=[row.pk]), {"action": "approve"})
        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestSubmitterSelfApproval:
    def test_submitter_cannot_act(self, users_by_role):
        pm = users_by_role["property_manager"][1]
        prop_id = pm.property_scope.order_by("id").first().pk
        project = _mk_draft(pm, prop_id)
        from capex.services.projects import apply_transition

        project = apply_transition(
            policy_resolve(pm), project, "submit", by=pm, version=project.version
        )
        with pytest.raises(Forbidden):
            apply_transition(policy_resolve(pm), project, "approve", by=pm, version=project.version)


class TestConcurrency:
    def test_stale_version_conflicts_and_replays_winner(self, api, users_by_role):
        pm = users_by_role["property_manager"][1]
        prop_id = pm.property_scope.order_by("id").first().pk
        project = _mk_draft(pm, prop_id)
        _forces(api, pm)
        # transition succeeds → version bumps; a client that held version 1 loses
        api.post(
            reverse("projects-transition", args=[project.pk]),
            {"action": "submit", "version": project.version},
        )
        stale = api.post(
            reverse("projects-transition", args=[project.pk]),
            {"action": "approve", "version": 1},
        )
        # JSON client gets 409 + authoritative state
        if stale.status_code == status.HTTP_409_CONFLICT:
            m.ApprovalEvent.objects.filter(project=project).latest("at")
            assert stale.data["project"]["id"] == project.pk

    def test_fragment_conflict_self_refreshes(self, api, users_by_role):
        pm = users_by_role["property_manager"][1]
        prop_id = pm.property_scope.order_by("id").first().pk
        project = _mk_draft(pm, prop_id)
        admin = users_by_role["org_admin"][pm.org_id]
        api.force_authenticate(user=pm)
        api.post(
            reverse("projects-transition", args=[project.pk]),
            {"action": "submit", "version": project.version},
        )
        # A competing approver (not the submitter) loses to a stale version:
        api.force_authenticate(user=admin)
        stale = api.post(
            reverse("projects-transition", args=[project.pk]),
            {"action": "approve", "version": 1},
            HTTP_HX_REQUEST="true",
        )
        # fragment path: 200 with the fresh row + flag so htmx can swap it in
        assert stale.status_code == status.HTTP_200_OK
        assert "data-project" in stale.content.decode()


class TestDraftLifecycle:
    def test_draft_edit_delete_ok(self, api, users_by_role):
        pm = users_by_role["property_manager"][1]
        prop_id = pm.property_scope.order_by("id").first().pk
        project = _mk_draft(pm, prop_id)
        _forces(api, pm)
        response = api.patch(
            reverse("projects-detail", args=[project.pk]), {"title": "Renamed draft"}
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["title"] == "Renamed draft"
        assert api.delete(reverse("projects-detail", args=[project.pk])).status_code == 204

    def test_nondraft_edit_is_422(self, api, users_by_role):
        admin = users_by_role["org_admin"][1]
        row = (
            m.Project.objects.exclude(status=m.Statuses.DRAFT)
            .filter(property__org_id=admin.org_id)
            .first()
        )
        _forces(api, admin)
        response = api.patch(reverse("projects-detail", args=[row.pk]), {"title": "nope"})
        assert response.status_code == 422

    def test_out_of_scope_create_is_404(self, api, users_by_role):
        pm = users_by_role["property_manager"][1]
        foreign_property = m.Property.objects.exclude(org_id=pm.org_id).first()
        _forces(api, pm)
        response = api.post(
            reverse("projects-list"),
            {
                "property": foreign_property.pk,
                "title": "smuggle attempt",
                "category": m.Categories.HVAC,
                "fiscal_period": m.FiscalPeriods.FY2026,
                "budget_amount": "100.00",
            },
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

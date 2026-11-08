"""Project capabilities: scoped CRUD + the approval state machine (SPEC §6).

Two rules make this file the *only* place workflow semantics exist:

1. **The transition table is declarative data.** An action not in the table
   raises :class:`TransitionNotAllowed` (the API surfaces it as 422). No
   handler-specific ``if status == ...`` logic anywhere else.
2. **Every write goes through the same narrow functions**, which take the
   caller's :class:`capex.policy.Entitlements`. Ownership checks (property in
   scope) happen here, so the API layer cannot forget them without skipping
   the service entirely.

Concurrency (SPEC §6): the compare-and-swap ``version`` guard and 409
path land with the concurrency step; until then writes are plain saves.
Rollup dirty-marking also lands with the rollups step (marked below).
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from django.db import transaction
from django.utils import timezone

from capex import models as m
from capex import policy

# --- Typed failures the API layer maps onto status codes --------------------


class ServiceError(Exception):
    """Base for expected, domain-level failures."""


class NotFound(ServiceError):
    """Row is out of the caller's scope (map to 404, never 403)."""


class NotAllowed(ServiceError):
    """Row visible but the action/transition is not permitted (422/403)."""


class Forbidden(NotAllowed):
    """Caller exists and can see the row, but the role may not act (403)."""


class ValidationError(ServiceError):
    """Semantics of the payload are wrong (map to 422 with a reason)."""


# --- The transition table (declarative; SPEC §6) -----------------------------


@dataclasses.dataclass(frozen=True)
class Transition:
    """One row of the state machine."""

    action: str           # API action name ("submit" | "approve" | "reject")
    to_status: str        # resulting status
    roles: Callable[[policy.Entitlements], bool]
    level: int            # approval stage of the *target* move (0 = submit)


_ACTIONS = {
    m.Statuses.DRAFT: (
        Transition("submit", m.Statuses.SUBMITTED, policy.can_submit, level=0),
    ),
    m.Statuses.SUBMITTED: (
        Transition("approve", m.Statuses.MANAGER_REVIEW, policy.can_approve, level=1),
        Transition("reject", m.Statuses.REJECTED, policy.can_approve, level=1),
    ),
    m.Statuses.MANAGER_REVIEW: (
        Transition("approve", m.Statuses.FINANCE_REVIEW, policy.can_approve, level=2),
        Transition("reject", m.Statuses.REJECTED, policy.can_approve, level=2),
    ),
    m.Statuses.FINANCE_REVIEW: (
        Transition("approve", m.Statuses.APPROVED, policy.can_approve, level=3),
        Transition("reject", m.Statuses.REJECTED, policy.can_approve, level=3),
    ),
    # approved / rejected are terminal: no outgoing rows by design.
}


def transitions_for(status: str) -> tuple[Transition, ...]:
    """The outgoing transitions a status allows (table = single source)."""
    return _ACTIONS.get(status, ())


# --- Scoped reads -----------------------------------------------------------


def list_for(ent, *, status: str | None = None, property_id: int | None = None,
             fiscal_period: str | None = None, category: str | None = None):
    """Projects the caller is entitled to, optionally filtered.

    ``property_id`` cannot widen scope: it is applied *after* the
    entitlement intersection (a foreign property id just yields nothing).
    """
    qs = policy.scoped(m.Project.objects.select_related("property"), ent)
    if status:
        qs = qs.filter(status=status)
    if property_id:
        qs = qs.filter(property_id=property_id)
    if fiscal_period:
        qs = qs.filter(fiscal_period=fiscal_period)
    if category:
        qs = qs.filter(category=category)
    return qs


def get_for(ent, pk: int) -> m.Project | None:
    """A single project *only if* entitled. ``None`` = not found/404."""
    return policy.scoped(m.Project.objects.select_related("property"), ent).filter(pk=pk).first()


def history_for(ent, project: m.Project):
    """The append-only event log, oldest first (drives the timeline UI)."""
    return policy.scoped(m.ApprovalEvent.objects.select_related("by_user"), ent).filter(
        project=project
    )


# --- Draft lifecycle (CRUD within scope) -------------------------------------


def _require_in_scope(ent, project: m.Project) -> None:
    """Raise NotFound if the row is not inside the caller's entitlements."""
    if ent is None or not ent.covers(project.property_id):
        # Deliberately identical outcome whether the id exists or not:
        # existence of foreign rows is never confirmed (SPEC §5.4).
        raise NotFound(f"no project {project.pk} in scope")


def _org_of_property(property_id: int) -> int | None:
    return (
        m.Property.objects.filter(pk=property_id).values_list("org_id", flat=True).first()
    )


def _mark_dirty(property_id: int) -> None:
    """Queue the rollup refresh (worker side of SPEC §7), after commit."""
    from capex import rollups

    org_id = _org_of_property(property_id)
    if org_id:
        transaction.on_commit(lambda: rollups.mark_dirty(org_id))


def create(ent, *, property_id: int, title: str, category: str, fiscal_period: str,
           budget_amount, actual_cost=None) -> m.Project:
    """Create a draft project on a property the caller may write."""
    if not policy.can_write(ent):
        raise Forbidden("role may not create projects")
    if not property_id or not ent.covers(property_id):
        raise NotFound(f"no property {property_id} in scope")
    if budget_amount is None or budget_amount < 0:
        raise ValidationError("budget_amount must be a non-negative amount")
    if actual_cost is not None and actual_cost < 0:
        raise ValidationError("actual_cost must be a non-negative amount")

    return m.Project.objects.create(
        property_id=property_id,
        title=title,
        category=category,
        fiscal_period=fiscal_period,
        budget_amount=budget_amount,
        actual_cost=actual_cost or 0,
    )


def update(ent, project: m.Project, **fields: Any) -> m.Project:
    """Edit the editable fields of a **draft** — drafts alone are editable."""
    _require_in_scope(ent, project)
    if project.status != m.Statuses.DRAFT:
        raise NotAllowed("only draft projects can be edited")
    allowed = {f for f in fields if f in {
        "title", "category", "fiscal_period", "budget_amount", "actual_cost",
    }}
    if "budget_amount" in fields and fields["budget_amount"] < 0:
        raise ValidationError("budget_amount must be a non-negative amount")
    for name in allowed:
        setattr(project, name, fields[name])
    project.save(update_fields=allowed or None)
    _mark_dirty(project.property_id)
    return project


def delete(ent, project: m.Project) -> None:
    """Delete a draft. Non-draft projects only ever terminate by rejection."""
    _require_in_scope(ent, project)
    if project.status != m.Statuses.DRAFT:
        raise NotAllowed("only draft projects can be deleted")
    pid = project.property_id
    project.delete()
    _mark_dirty(pid)


# --- The state machine (one real path end to end; SPEC §6) -------------------


def apply_transition(ent, project: m.Project, action: str, *, by) -> m.Project:
    """Perform ``action`` on ``project``, validating the table + roles.

    Caller contract: ``project`` came from :func:`get_for` (already scoped).
    """
    _require_in_scope(ent, project)

    options = {t.action: t for t in transitions_for(project.status)}
    if action not in options:
        raise NotAllowed(
            f"transition {action!r} not allowed from status {project.status!r}"
        )
    transition = options[action]

    if not transition.roles(ent):
        raise Forbidden(f"role {ent.role!r} may not {action}")
    # Nobody acts on their own request, no matter their role (SPEC §6).
    if action != "submit" and project.submitted_by_id == by.pk:
        raise Forbidden("a submission may not be approved by its submitter")
    if action == "submit":
        project.submitted_by = by

    with transaction.atomic():
        project.status = transition.to_status
        project.save(update_fields=["status", "submitted_by", "updated_at"])
        m.ApprovalEvent.objects.create(
            project=project,
            property=project.property,
            level=transition.level,
            to_status=transition.to_status,
            by_user=by,
            at=timezone.now(),
        )
        _mark_dirty(project.property_id)
    return project

"""Entitlements + row-level scoping — the single authorization brain (SPEC §5).

Every model-facing query in the codebase is scoped through this module; no
view, serializer, or service may hand-build a tenant predicate. Lectures on
why it is shaped like this live in DESIGN.md §2; the essence:

* ``resolve(user)`` computes the caller's entitlements **from the database**
  on every request. JWT claims are identity, never authorization — a forged
  role/scope claim in a token changes nothing here.
* ``scoped(qs, ent)`` narrows any queryset of a model that has a ``property``
  relation. For admins it filters by ``property__org``, for everyone else by
  the exact property subset. A corrupted row (e.g. a scope entry pointing at
  another org's property) still cannot leak: non-admin scopes are
  intersected with the caller's own org.
* Role predicates are the only place the four roles' capabilities are
  spelled out; the API/fragment layers consult them, they never re-derive.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from capex import models as m
from capex.models import Roles


#: Sentinel entitlements class for admins who are entitled to every property
#: of their org. Using a dataclass field instead of a magic empty set makes
#: "which properties?" unambiguous at call sites.
@dataclasses.dataclass(frozen=True, slots=True)
class Entitlements:
    org_id: Optional[int]
    role: str
    #: The exact property ids within the org, or ``ALL`` for org admins.
    property_ids: frozenset
    read_only: bool

    def covers(self, property_id: int) -> bool:
        """Whether a row on this property is in scope (ignores org check —
        callers use :func:`scoped` for the combined filter)."""
        if self.property_ids is ALL:
            return True
        return property_id in self.property_ids


class _AllProperties:
    """Singleton meaning 'every property of my org' (admins only)."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<ALL properties of org>"

    def __bool__(self) -> bool:
        return True


ALL = _AllProperties()


def resolve(user) -> Optional[Entitlements]:
    """Compute the caller's entitlements, or ``None`` when untitled.

    ``None`` is only for platform superusers with no org — they are visible
    to the admin site but must never see tenant data through the app; every
    view that receives ``None`` responds as if the user had no grants at all.
    """
    # Cheap per-request memo: the user instance is fetched fresh on each
    # request, so the cache lives exactly as long as that request's user.
    try:
        return user._capex_entitlements
    except AttributeError:
        pass

    ent = _compute(user)
    user._capex_entitlements = ent
    return ent


def _compute(user) -> Optional[Entitlements]:
    if getattr(user, "is_superuser", False) and user.org_id is None:
        return None

    if user.org_id is None:  # regular user missing org — least privilege
        role = ""
        ids: _AllProperties | set = set()
    elif user.role == Roles.ORG_ADMIN:
        role = user.role
        ids = ALL
    else:
        role = user.role
        # Hardened against bad rows: intersect the granted scope with the
        # caller's own org so a corrupted M2M row cannot cross orgs.
        ids = set(
            m.Property.objects.filter(user__pk=user.pk, org_id=user.org_id).values_list(
                "id", flat=True
            )
        )

    return Entitlements(
        org_id=user.org_id if user.org_id is not None else None,
        role=role,
        property_ids=frozenset(ids) if not isinstance(ids, _AllProperties) else ids,
        read_only=is_read_only_role(role),
    )


# --- Role predicates (single source of role capabilities) -------------------

_WRITER_ROLES = (Roles.ORG_ADMIN, Roles.PROPERTY_MANAGER)
_APPROVER_ROLES = (Roles.ORG_ADMIN, Roles.APPROVER)


def is_read_only_role(role: str) -> bool:
    """Viewers (and anyone unlisted) cannot write."""
    return role not in _WRITER_ROLES


def can_write(ent: Entitlements) -> bool:
    """Full CRUD on own rows: property managers and org admins only."""
    return ent is not None and ent.role in _WRITER_ROLES


def can_approve(ent: Entitlements) -> bool:
    """May act on approval levels: approvers and org admins."""
    return ent is not None and ent.role in _APPROVER_ROLES


def can_submit(ent: Entitlements) -> bool:
    """Draft → submitted: property managers and org admins."""
    return can_write(ent)


# --- Queryset scoping --------------------------------------------------------

def scoped(qs, ent: Optional[Entitlements]):
    """Narrow *any* queryset of a model with a ``property`` relation.

    Nothing about the underlying queryset is trusted: this intersection is
    applied on top of whatever the caller already filtered — so even a
    careless upstream ``Project.objects.all()`` stays safe once it passes
    through here.
    """
    if ent is None:
        return qs.none()
    if ent.property_ids is ALL:
        return qs.filter(property__org_id=ent.org_id)
    return qs.filter(property_id__in=ent.property_ids)


def scoped_properties(qs, ent: Optional[Entitlements]):
    """Narrow a queryset of Property rows themselves."""
    if ent is None:
        return qs.none()
    if ent.property_ids is ALL:
        return qs.filter(org_id=ent.org_id)
    return qs.filter(pk__in=ent.property_ids)

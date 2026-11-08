"""Domain models for the multi-tenant CapEx platform.

Scoping invariants (enforced centrally in capex.policy):

* Tenant data reaches its org in one indexed hop: ``property.org``.
  (``User.org`` is the lone exception — an admin's tenancy cannot be derived
  from a property grant.)
* ``property`` is the atom of entitlement. Non-admins are granted a subset of
  their org's properties via ``User.property_scope``; org admins resolve to
  the whole org at request time.
* ``ApprovalEvent`` is append-only: the state machine mutates
  ``Project.status`` and appends events; history is never rewritten.
* Analytics tables are deliberately NOT here yet — ProjectRollup and
  DashboardStamp arrive with the rollup step (SPEC §7).

Seed ids from the harness JSON are inserted verbatim; auto-field sequences
are untouched (the loader creates every row explicitly).
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class Roles(models.TextChoices):
    """Grant scopes — see SPEC §5 and the harness README role matrix."""

    ORG_ADMIN = "org_admin", "Org admin"
    PROPERTY_MANAGER = "property_manager", "Property manager"
    APPROVER = "approver", "Approver"
    VIEWER = "viewer", "Viewer"


class Statuses(models.TextChoices):
    """Project lifecycle; the forward flow is SPEC §6's transition table."""

    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    MANAGER_REVIEW = "manager_review", "Manager review"
    FINANCE_REVIEW = "finance_review", "Finance review"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class FiscalPeriods(models.TextChoices):
    """Harness-planted periods; new periods are append-only by convention."""

    FY2024 = "FY2024", "FY2024"
    FY2025 = "FY2025", "FY2025"
    FY2026 = "FY2026", "FY2026"


class Categories(models.TextChoices):
    """Asset categories; includes the catch-all for anything uncatalogued."""

    HVAC = "HVAC", "HVAC"
    ROOFING = "Roofing", "Roofing"
    ELECTRICAL = "Electrical", "Electrical"
    PLUMBING = "Plumbing", "Plumbing"
    IT_NETWORK = "IT/Network", "IT/Network"
    FF_AND_E = "Furniture/FF&E", "Furniture / FF&E"
    ELEVATORS = "Elevators", "Elevators"
    LIFE_SAFETY = "Life Safety", "Life Safety"
    LANDSCAPING = "Landscaping", "Landscaping"
    RENOVATION = "Renovation", "Renovation"
    OTHER = "Other", "Other"


class Organization(models.Model):
    """A tenant. Every isolation rule keys off this row's id."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"


class Property(models.Model):
    """A sub-unit of an org (building/hotel) — the atom of user grants."""

    org = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="properties")
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=32)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["org", "name"], name="uniq_property_name_per_org"),
            models.UniqueConstraint(fields=["org", "code"], name="uniq_property_code_per_org"),
        ]
        indexes = [models.Index(fields=["org"])]

    def __str__(self) -> str:
        return f"{self.name} [{self.code}]"


class User(AbstractUser):
    """Auth + entitlement carrier.

    ``org`` is null only for platform superusers, who sit outside the tenancy
    model entirely. Admin scope is *resolved* from ``org`` at request time —
    never stored — so an admin's grant stays correct when properties are
    added later.
    """

    email = models.EmailField(unique=True)
    org = models.ForeignKey(
        Organization, on_delete=models.PROTECT, null=True, blank=True, related_name="users"
    )
    role = models.CharField(max_length=32, choices=Roles.choices, blank=True)
    property_scope = models.ManyToManyField(Property, blank=True, related_name="users")

    class Meta:
        indexes = [models.Index(fields=["org"])]

    def __str__(self) -> str:
        return self.email or self.username


class Project(models.Model):
    """A spending request moving through the approval state machine."""

    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="projects")
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=64, choices=Categories.choices)
    fiscal_period = models.CharField(max_length=16, choices=FiscalPeriods.choices)
    budget_amount = models.DecimalField(max_digits=16, decimal_places=2)
    actual_cost = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    status = models.CharField(max_length=16, choices=Statuses.choices, default=Statuses.DRAFT)
    # Optimistic concurrency guard (SPEC §6): at each cascade transition the
    # API filters on the version the client saw; a loser of the race gets a
    # 409 with the fresh row. Present as of the concurrency step commit.
    version = models.PositiveIntegerField(default=1)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submitted_projects",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["property", "fiscal_period", "category"]),
            models.Index(fields=["property", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.id}, {self.status})"


class ApprovalEvent(models.Model):
    """Append-only state-machine history; feeds approval cycle time.

    ``level`` is the approval stage at which the event happened
    (1 = manager review, 2 = finance review); ``to_status`` records where
    the project moved. Timestamps are explicit so the seeder can load the
    harness history verbatim.
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="history")
    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="approval_events")
    level = models.PositiveSmallIntegerField()
    to_status = models.CharField(max_length=16, choices=Statuses.choices)
    by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approval_events",
    )
    at = models.DateTimeField()

    class Meta:
        ordering = ["at", "id"]
        indexes = [
            models.Index(fields=["project", "at"]),
            models.Index(fields=["property"]),
        ]

    def __str__(self) -> str:
        return f"#{self.project_id} → {self.to_status} @ {self.at:%Y-%m-%d %H:%M}"


class Budget(models.Model):
    """Planned allocation for a property within one fiscal period."""

    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="budgets")
    fiscal_period = models.CharField(max_length=16, choices=FiscalPeriods.choices)
    allocated_amount = models.DecimalField(max_digits=16, decimal_places=2)

    class Meta:
        ordering = ["fiscal_period"]
        constraints = [
            models.UniqueConstraint(fields=["property", "fiscal_period"], name="uniq_budget_grain")
        ]

    def __str__(self) -> str:
        return f"{self.property_id} · {self.fiscal_period} · {self.allocated_amount}"


class ProjectRollup(models.Model):
    """Precomputed analytics grain (SPEC §7) — the dashboard never aggregates
    Project live.

    Written only by capex.rollups (worker): a debounced, org-scoped, full
    recompute per dirty org inside one transaction (correct-at-rest, ~100ms
    at big-tenant scale), unique on the grain. Scoping goes through
    ``property`` like every other tenant row.
    """

    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="project_rollups")
    fiscal_period = models.CharField(max_length=16, choices=FiscalPeriods.choices)
    category = models.CharField(max_length=64, choices=Categories.choices)

    project_count = models.PositiveIntegerField(default=0)
    budget_sum = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    actual_sum = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    # Projects currently sitting in an approval stage (backlog input).
    pending_count = models.PositiveIntegerField(default=0)
    # Projects that reached a terminal decision, with total decision
    # seconds = the cycle-time numerator; denominator = decided_count.
    decided_count = models.PositiveIntegerField(default=0)
    cycle_seconds_sum = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["property", "fiscal_period", "category"], name="uniq_rollup_grain"
            )
        ]

    def __str__(self) -> str:
        return f"rollup {self.property_id} · {self.fiscal_period} · {self.category}"


class DashboardStamp(models.Model):
    """Per-org monotonic version, bumped atomically on every rollup write.

    Carried into cache keys (versioned keys make invalidation implicit) and
    SSE notifications so UIs can detect staleness cheaply.
    """

    org = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="dashboard_stamp"
    )
    version = models.BigIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"stamp org {self.org_id} v{self.version}"


#: Statuses that count towards the approval backlog KPI (SPEC §7).
PENDING_STATUSES = (
    Statuses.SUBMITTED,
    Statuses.MANAGER_REVIEW,
    Statuses.FINANCE_REVIEW,
)

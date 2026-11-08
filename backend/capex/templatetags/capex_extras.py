"""Template filters for the CapEx fragments.

Kept deliberately tiny: badge classes and money/variance formatting that
both the API fragments and the page templates reuse. Tokens/typography
rules live in the CSS layer; these filters only decide semantics
(pos/neg direction, status vocabulary).
"""

from __future__ import annotations

from decimal import Decimal

from django import template

from capex.models import Statuses

register = template.Library()

STANDARD_DISPLAYS = dict(Statuses.choices)


@register.filter
def badge_class(status: str) -> str:
    """CSS class for a project status badge (mirrors the mockup tokens)."""
    return {
        "draft": "b-draft",
        "submitted": "b-submitted",
        "manager_review": "b-manager",
        "finance_review": "b-finance",
        "approved": "b-approved",
        "rejected": "b-rejected",
    }.get(status, "b-draft")


@register.filter
def money(value) -> str:
    """Comma-grouped money columns (mono figures, per the design tokens)."""
    try:
        return f"{Decimal(value):,.2f}"
    except Exception:  # pragma: no cover - display-only
        return "—"


def _budget_of(project) -> Decimal:
    raw = project.budget_amount
    return Decimal(str(raw)) if raw is not None else Decimal(0)


def _actual_of(project) -> Decimal:
    raw = project.actual_cost
    return Decimal(str(raw)) if raw is not None else Decimal(0)


@register.filter
def signed_pct(project) -> str:
    """Human variance percentage: +7% / −10% (over is positive)."""
    budget, actual = _budget_of(project), _actual_of(project)
    if not budget:
        return "—"
    pct = (actual - budget) / budget * 100
    sign = "+" if pct > 0 else ("−" if pct < 0 else "")
    quarter = Decimal("0.01")
    return f"{sign}{abs(pct.quantize(quarter)):.2f}%"


@register.filter
def status_display(value: str) -> str:
    """Readable name for a status code (fragments + timeline)."""
    return STANDARD_DISPLAYS.get(value, value.replace("_", " "))


@register.filter
def variance_over(project) -> bool:
    """True when the project is spending above its budget."""
    return _actual_of(project) > _budget_of(project)

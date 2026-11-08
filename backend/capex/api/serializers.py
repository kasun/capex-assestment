"""API payload schemas.

Read serializers carry presentation-computed fields (variance) so JSON and
fragment views render the *same* numbers from the same place. Write
serializers validate input; the services do domain checks.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from rest_framework import serializers

from capex import models as m


class PropertyLiteSerializer(serializers.ModelSerializer):
    org_name = serializers.CharField(source="org.name", read_only=True)

    class Meta:
        model = m.Property
        fields = ("id", "name", "code", "org_name")
        read_only_fields = fields


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    if not denominator:
        return Decimal("0.00")
    return (numerator / denominator * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class ProjectSerializer(serializers.ModelSerializer):
    """Read representation — identical source of truth for JSON and fragments."""

    property_detail = PropertyLiteSerializer(source="property", read_only=True)
    submitted_by = serializers.CharField(
        source="submitted_by.email", read_only=True, default=None
    )
    variance = serializers.SerializerMethodField()
    variance_pct = serializers.SerializerMethodField()

    class Meta:
        model = m.Project
        fields = (
            "id",
            "property",
            "property_detail",
            "title",
            "category",
            "fiscal_period",
            "budget_amount",
            "actual_cost",
            "variance",
            "variance_pct",
            "status",
            "submitted_by",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields  # writes go through services, not serializers

    def get_variance(self, obj) -> str:
        diff = obj.actual_cost - obj.budget_amount
        return f"{'+' if diff >= 0 else '−'}{abs(diff):,.0f}"

    def get_variance_pct(self, obj) -> str:
        pct = _pct((obj.actual_cost - obj.budget_amount) * -1, obj.budget_amount)
        sign = "+" if obj.actual_cost > obj.budget_amount else "−"
        return f"{sign}{abs(pct)}%"


class ProjectWriteSerializer(serializers.Serializer):
    """Create/update input. Domain rules stay in services; this is shape+types."""

    property = serializers.IntegerField(min_value=1)
    title = serializers.CharField(max_length=255)
    category = serializers.ChoiceField(choices=m.Categories.choices)
    fiscal_period = serializers.ChoiceField(choices=m.FiscalPeriods.choices)
    budget_amount = serializers.DecimalField(max_digits=16, decimal_places=2, min_value=0)
    actual_cost = serializers.DecimalField(
        max_digits=16, decimal_places=2, min_value=0, required=False, default=None
    )


class TransitionInputSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("submit", "approve", "reject"))

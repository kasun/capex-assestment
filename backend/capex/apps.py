from django.apps import AppConfig


class CapexConfig(AppConfig):
    """Multi-tenant CapEx models, policy, services, API, and UI."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "capex"
    verbose_name = "CapEx Platform"

    def ready(self) -> None:
        """Register the django-q2 schedule (aggregation reconcile sweep)."""
        from capex import scheduling

        scheduling.ensure_schedules()

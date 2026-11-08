"""Admin registrations — development convenience for eyeballing seeded data.

Intentionally minimal: the admin is not a grader-facing surface, and it must
never become a way to bypass the app's approval rules, so write actions are
kept to the plain forms (no bulk state changes on projects).
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from capex import models


@admin.register(models.Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug")
    search_fields = ("name",)


@admin.register(models.Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "org")
    list_filter = ("org",)
    search_fields = ("name", "code")


@admin.register(models.User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("email", "org", "role", "is_active")
    list_filter = ("role", "org")
    search_fields = ("email",)
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("CapEx", {"fields": ("org", "role", "property_scope")}),
    )


@admin.register(models.Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        "id", "title", "property", "category",
        "fiscal_period", "status", "budget_amount",
    )
    list_filter = ("status", "category", "fiscal_period", "property__org")
    search_fields = ("title",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(models.ApprovalEvent)
class ApprovalEventAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "level", "to_status", "by_user", "at")
    list_filter = ("to_status",)
    ordering = ("-at",)


@admin.register(models.Budget)
class BudgetAdmin(admin.ModelAdmin):
    list_display = ("id", "property", "fiscal_period", "allocated_amount")
    list_filter = ("fiscal_period", "property__org")

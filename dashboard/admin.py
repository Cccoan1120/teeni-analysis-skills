from django.contrib import admin

from .models import DailySnapshot, LoginThrottle, ManagementInsight, ManagementInsightRevision


@admin.register(DailySnapshot)
class DailySnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "data_date",
        "scene_id",
        "core_version",
        "rules_version",
        "published_at",
    )
    readonly_fields = ("published_at",)
    ordering = ("-data_date",)


@admin.register(LoginThrottle)
class LoginThrottleAdmin(admin.ModelAdmin):
    list_display = ("identifier_hash", "failures", "locked_until", "updated_at")
    readonly_fields = ("identifier_hash", "failures", "locked_until", "updated_at")
    ordering = ("-updated_at",)


class ManagementInsightRevisionInline(admin.TabularInline):
    model = ManagementInsightRevision
    fields = ("revision", "title", "created_by", "created_at")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(ManagementInsight)
class ManagementInsightAdmin(admin.ModelAdmin):
    list_display = ("start_date", "end_date", "status", "current_revision", "published_at", "published_by")
    readonly_fields = ("current_revision", "published_revision", "created_at", "updated_at", "published_at")
    ordering = ("-updated_at",)
    inlines = (ManagementInsightRevisionInline,)

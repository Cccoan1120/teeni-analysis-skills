from django.contrib import admin

from .models import TopicArtifact, TopicAuditEvent, TopicJob, TopicSnapshot


class TopicArtifactInline(admin.TabularInline):
    model = TopicArtifact
    extra = 0
    readonly_fields = ("kind", "download_name", "sha256", "byte_size", "verified", "expires_at")


@admin.register(TopicJob)
class TopicJobAdmin(admin.ModelAdmin):
    list_display = ("data_date", "status", "progress", "created_by", "created_at", "expires_at")
    list_filter = ("status", "data_date")
    search_fields = ("id", "original_name", "input_sha256")
    readonly_fields = ("created_at", "updated_at", "started_at", "finished_at")
    inlines = (TopicArtifactInline,)


@admin.register(TopicSnapshot)
class TopicSnapshotAdmin(admin.ModelAdmin):
    list_display = ("data_date", "taxonomy_version", "catalog_revision", "published_at")
    readonly_fields = ("published_at",)


@admin.register(TopicAuditEvent)
class TopicAuditEventAdmin(admin.ModelAdmin):
    list_display = ("job", "action", "actor", "created_at")
    readonly_fields = ("job", "action", "actor", "details", "created_at")

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from interest_engine.registry import (
    BroadTopic,
    EntitySubtype,
    EntityType,
    SafetyCategory,
    Visibility,
    normalize_text,
)
from dashboard.product_versions import DEFAULT_PRODUCT_VERSION, PRODUCT_VERSION_CHOICES
from topics.models import TopicJob, validate_relative_path


def new_registry_id() -> str:
    return f"IE{uuid.uuid4().hex[:12].upper()}"


class InterestEntity(models.Model):
    class Status(models.TextChoices):
        APPROVED = "approved", "已批准"
        MERGED = "merged", "已合并"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    registry_id = models.CharField(max_length=16, unique=True, default=new_registry_id)
    canonical_name = models.CharField(max_length=80, unique=True)
    normalized_name = models.CharField(max_length=80, unique=True, editable=False)
    entity_type = models.CharField(max_length=32, choices=[(item.value, item.value) for item in EntityType])
    entity_subtype = models.CharField(
        max_length=32,
        choices=[(item.value, item.value) for item in EntitySubtype],
        default=EntitySubtype.OTHER_MEME,
    )
    broad_topic = models.CharField(max_length=40, choices=[(item.value, item.value) for item in BroadTopic])
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="interest_children",
    )
    source_label = models.CharField(max_length=80, blank=True)
    source_platform = models.CharField(max_length=80, blank=True)
    visibility = models.CharField(
        max_length=16,
        choices=[(item.value, item.value) for item in Visibility],
        default=Visibility.PUBLIC,
    )
    safety_category = models.CharField(
        max_length=32,
        choices=[(item.value, item.value) for item in SafetyCategory],
        default=SafetyCategory.NONE,
    )
    match_rules = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.APPROVED)
    merge_target = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="merged_entities")
    registry_order = models.PositiveIntegerField(default=100000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["registry_order", "registry_id"]

    def clean(self) -> None:
        normalized = normalize_text(self.canonical_name)
        if normalized and InterestAlias.objects.filter(normalized=normalized).exists():
            raise ValidationError("canonical name conflicts with an existing alias")
        if self.status == self.Status.MERGED and not self.merge_target_id:
            raise ValidationError("merged entities require a merge target")
        if self.merge_target_id == self.id:
            raise ValidationError("an entity cannot merge into itself")
        depth = 1
        seen = {self.pk} if self.pk else set()
        parent = self.parent
        while parent is not None:
            if parent.pk in seen:
                raise ValidationError("entity parent hierarchy must be acyclic")
            seen.add(parent.pk)
            depth += 1
            if depth > 3:
                raise ValidationError("entity parent hierarchy may contain at most three levels")
            parent = parent.parent

    def save(self, *args, **kwargs):
        self.canonical_name = self.canonical_name.strip()
        self.normalized_name = normalize_text(self.canonical_name)
        if not self.normalized_name:
            raise ValidationError("canonical name is required")
        return super().save(*args, **kwargs)


class InterestAlias(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity = models.ForeignKey(InterestEntity, on_delete=models.CASCADE, related_name="aliases")
    value = models.CharField(max_length=80)
    normalized = models.CharField(max_length=80, unique=True, editable=False)
    ambiguous = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-ambiguous", "normalized"]

    def clean(self) -> None:
        normalized = normalize_text(self.value)
        if normalized and InterestEntity.objects.exclude(pk=self.entity_id).filter(normalized_name=normalized).exists():
            raise ValidationError("alias conflicts with an existing canonical name")

    def save(self, *args, **kwargs):
        self.value = self.value.strip()
        self.normalized = normalize_text(self.value)
        if not self.normalized:
            raise ValidationError("alias is required")
        if self.normalized == self.entity.normalized_name:
            raise ValidationError("canonical name must not be repeated as an alias")
        return super().save(*args, **kwargs)


class InterestRegistrySnapshot(models.Model):
    job = models.OneToOneField(TopicJob, on_delete=models.CASCADE, related_name="interest_registry_snapshot")
    registry_version = models.CharField(max_length=32)
    sha256 = models.CharField(max_length=64)
    relative_path = models.CharField(max_length=500, validators=[validate_relative_path])
    created_at = models.DateTimeField(auto_now_add=True)


class InterestCandidate(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已批准"
        MERGED = "merged", "已合并"
        REJECTED = "rejected", "已拒绝"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(TopicJob, on_delete=models.CASCADE, related_name="interest_candidates")
    data_date = models.DateField()
    phrase = models.CharField(max_length=80)
    normalized_phrase = models.CharField(max_length=80)
    query_count = models.PositiveIntegerField()
    users = models.PositiveIntegerField()
    sessions = models.PositiveIntegerField()
    seven_day_growth = models.FloatField(null=True, blank=True)
    suggested_name = models.CharField(max_length=80, blank=True)
    suggested_type = models.CharField(max_length=32, blank=True)
    suggested_subtype = models.CharField(max_length=32, blank=True)
    suggested_parent_name = models.CharField(max_length=80, blank=True)
    suggested_aliases = models.JSONField(default=list, blank=True)
    model_confidence = models.CharField(max_length=16, blank=True)
    model_recommended = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    resolved_entity = models.ForeignKey(
        InterestEntity, null=True, blank=True, on_delete=models.PROTECT, related_name="resolved_candidates"
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="interest_reviews"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-data_date", "-users", "-sessions", "normalized_phrase"]
        constraints = [
            models.UniqueConstraint(fields=["job", "normalized_phrase"], name="unique_interest_candidate_per_job")
        ]


class InterestSnapshot(models.Model):
    product_version = models.CharField(max_length=2, choices=PRODUCT_VERSION_CHOICES, default=DEFAULT_PRODUCT_VERSION)
    data_date = models.DateField()
    job = models.OneToOneField(TopicJob, on_delete=models.PROTECT, related_name="interest_snapshot")
    schema_version = models.CharField(max_length=64)
    engine_version = models.CharField(max_length=64)
    source_sha256 = models.CharField(max_length=64)
    registry_sha256 = models.CharField(max_length=64)
    workbook_sha256 = models.CharField(max_length=64)
    payload = models.JSONField()
    published_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["data_date"]
        constraints = [models.UniqueConstraint(fields=["product_version", "data_date"], name="unique_interest_snapshot_version_date")]


class InterestSegmentSnapshot(models.Model):
    product_version = models.CharField(max_length=2, choices=PRODUCT_VERSION_CHOICES, default=DEFAULT_PRODUCT_VERSION)
    data_date = models.DateField()
    job = models.ForeignKey(TopicJob, on_delete=models.PROTECT, related_name="interest_segment_snapshots")
    dimension_set = models.CharField(max_length=32)
    schema_version = models.CharField(max_length=64)
    source_sha256 = models.CharField(max_length=64)
    registry_sha256 = models.CharField(max_length=64)
    province_map_version = models.CharField(max_length=64)
    province_map_sha256 = models.CharField(max_length=64)
    payload = models.JSONField()
    published_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["data_date", "dimension_set"]
        constraints = [
            models.UniqueConstraint(
                fields=["product_version", "data_date", "dimension_set"],
                name="unique_interest_segment_version_date_dim",
            )
        ]


class InterestDailyContribution(models.Model):
    product_version = models.CharField(max_length=2, choices=PRODUCT_VERSION_CHOICES, default=DEFAULT_PRODUCT_VERSION)
    data_date = models.DateField()
    source_sha256 = models.CharField(max_length=64)
    registry_sha256 = models.CharField(max_length=64)
    identity_sha256 = models.CharField(max_length=64)
    detail_sha256 = models.CharField(max_length=64)
    payload = models.BinaryField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["data_date"]
        constraints = [models.UniqueConstraint(fields=["product_version", "data_date"], name="unique_interest_contribution_version_date")]


class InterestBackfillRequest(models.Model):
    product_version = models.CharField(max_length=2, choices=PRODUCT_VERSION_CHOICES, default=DEFAULT_PRODUCT_VERSION)
    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        RUNNING = "running", "处理中"
        COMPLETED = "completed", "已完成"
        FAILED = "failed", "失败"

    entity = models.ForeignKey(InterestEntity, on_delete=models.PROTECT, related_name="backfill_requests")
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["created_at"]


class InterestReviewEvent(models.Model):
    candidate = models.ForeignKey(InterestCandidate, on_delete=models.PROTECT, related_name="review_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=32)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("review events are immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("review events are immutable")

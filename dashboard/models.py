import uuid

from django.conf import settings
from django.db import models

from .product_versions import DEFAULT_PRODUCT_VERSION, PRODUCT_VERSION_CHOICES


class DailySnapshot(models.Model):
    data_date = models.DateField()
    product_version = models.CharField(
        max_length=2,
        choices=PRODUCT_VERSION_CHOICES,
        default=DEFAULT_PRODUCT_VERSION,
    )
    scene_id = models.CharField(max_length=16)
    core_version = models.CharField(max_length=32)
    rules_version = models.CharField(max_length=32)
    contract_version = models.CharField(max_length=64)
    source_sha256 = models.CharField(max_length=64)
    workbook_sha256 = models.CharField(max_length=64)
    payload = models.JSONField()
    published_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product_version", "data_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["data_date", "product_version"],
                name="unique_daily_snapshot_product_date",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(product_version="M1", scene_id="488")
                    | models.Q(product_version="M2", scene_id="904")
                ),
                name="daily_snapshot_product_scene_matches",
            ),
        ]

    def __str__(self) -> str:
        return f"Teeni {self.product_version} snapshot {self.data_date.isoformat()}"


class LoginThrottle(models.Model):
    identifier_hash = models.CharField(max_length=64, unique=True)
    failures = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Login throttle {self.identifier_hash[:12]}"


class ProductResearchSnapshot(models.Model):
    product_version = models.CharField(max_length=2, choices=PRODUCT_VERSION_CHOICES)
    data_date = models.DateField()
    payload = models.JSONField()
    source_sha256 = models.CharField(max_length=64)
    published_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["product_version", "data_date"], name="unique_research_product_date")]


class ManagementInsight(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        WITHDRAWN = "withdrawn", "已撤回"

    class Origin(models.TextChoices):
        AUTOMATIC = "automatic", "自动生成"
        MANUAL = "manual", "手动创建"
        INITIAL = "initial", "首期专题"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product_version = models.CharField(
        max_length=2,
        choices=PRODUCT_VERSION_CHOICES,
        default=DEFAULT_PRODUCT_VERSION,
    )
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.MANUAL)
    current_revision = models.PositiveIntegerField(default=0)
    published_revision = models.PositiveIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_management_insights",
    )
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="published_management_insights",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-published_at", "-updated_at"]

    def __str__(self) -> str:
        return f"{self.product_version} management insight {self.start_date} to {self.end_date}"


class ManagementInsightRevision(models.Model):
    insight = models.ForeignKey(ManagementInsight, on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    title = models.CharField(max_length=140)
    summary = models.TextField()
    facts = models.JSONField(default=list)
    mechanisms = models.JSONField(default=list)
    associations = models.JSONField(default=list)
    hypotheses = models.JSONField(default=list)
    actions = models.JSONField(default=list)
    metric_snapshot = models.JSONField(default=dict)
    source_hashes = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="management_insight_revisions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(fields=["insight", "revision"], name="unique_management_insight_revision")
        ]

    def __str__(self) -> str:
        return f"{self.insight_id} revision {self.revision}"

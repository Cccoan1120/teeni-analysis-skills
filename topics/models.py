import uuid
from datetime import timedelta
from pathlib import PurePosixPath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from dashboard.product_versions import DEFAULT_PRODUCT_VERSION, PRODUCT_VERSION_CHOICES, PRODUCT_VERSION_SCENES


def default_expiry():
    return timezone.now() + timedelta(days=settings.TEENI_TOPIC_RETENTION_DAYS)


def validate_relative_path(value: str) -> None:
    candidate = PurePosixPath(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts or "\\" in value:
        raise ValidationError("artifact path must be a safe relative POSIX path")


class TopicJob(models.Model):
    class Pipeline(models.TextChoices):
        LEGACY_TOPIC = "legacy_topic", "旧话题分析"
        SHADOW = "shadow", "兴趣与旧话题影子双跑"
        INTEREST_V1 = "interest_v1", "IP兴趣分析 v1"

    class Status(models.TextChoices):
        UPLOADING = "uploading", "上传中"
        VALIDATING = "validating", "本地校验"
        BASE_RUNNING = "base_running", "基础分析"
        BASE_VERIFYING = "base_verifying", "基础验证"
        QUEUED = "queued", "排队"
        SEMANTIC_RUNNING = "semantic_running", "语义分析"
        INTEREST_RUNNING = "interest_running", "本地兴趣分析"
        INTEREST_VERIFYING = "interest_verifying", "兴趣结果验证"
        FINAL_VERIFYING = "final_verifying", "最终验证"
        PUBLISHING = "publishing", "发布"
        COMPLETED = "completed", "发布完成"
        WAITING_MODEL = "waiting_model", "等待模型"
        FAILED = "failed", "失败"
        EXPIRED = "expired", "已过期"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="topic_jobs")
    data_date = models.DateField()
    product_version = models.CharField(max_length=2, choices=PRODUCT_VERSION_CHOICES, default=DEFAULT_PRODUCT_VERSION)
    original_name = models.CharField(max_length=255)
    pipeline = models.CharField(max_length=24, choices=Pipeline.choices, default=Pipeline.INTEREST_V1)
    expected_size = models.BigIntegerField()
    received_size = models.BigIntegerField(default=0)
    expected_sha256 = models.CharField(max_length=64, blank=True)
    input_sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.UPLOADING)
    progress = models.PositiveSmallIntegerField(default=0)
    runtime_state = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=240, blank=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(default=default_expiry)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"]), models.Index(fields=["data_date"])]

    @property
    def scene_id(self):
        return PRODUCT_VERSION_SCENES[self.product_version]

    def __str__(self) -> str:
        return f"Topic job {self.data_date.isoformat()} {self.id}"


class TopicArtifact(models.Model):
    class Kind(models.TextChoices):
        TOPIC_WORKBOOK = "topic_workbook", "话题报告"
        TOPIC_DETAIL = "topic_detail", "隐私安全明细"
        TOPIC_DETAIL_ZIP = "topic_detail_zip", "明细压缩包"
        INTEREST_WORKBOOK = "interest_workbook", "IP兴趣报告"
        INTEREST_DETAIL_ZIP = "interest_detail_zip", "兴趣隐私安全明细"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(TopicJob, on_delete=models.CASCADE, related_name="artifacts")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    relative_path = models.CharField(max_length=500, validators=[validate_relative_path])
    download_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    byte_size = models.BigIntegerField()
    verified = models.BooleanField(default=False)
    expires_at = models.DateTimeField(default=default_expiry)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class TopicSnapshot(models.Model):
    data_date = models.DateField(unique=True)
    job = models.OneToOneField(TopicJob, on_delete=models.PROTECT, related_name="snapshot")
    taxonomy_version = models.CharField(max_length=32)
    catalog_revision = models.CharField(max_length=32)
    report_schema = models.CharField(max_length=64)
    source_sha256 = models.CharField(max_length=64)
    workbook_sha256 = models.CharField(max_length=64)
    payload = models.JSONField()
    published_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["data_date"]


class TopicAuditEvent(models.Model):
    job = models.ForeignKey(TopicJob, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="topic_audit_events",
    )
    action = models.CharField(max_length=64)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

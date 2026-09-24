import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0002_loginthrottle"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ManagementInsight",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("status", models.CharField(choices=[("draft", "草稿"), ("published", "已发布"), ("withdrawn", "已撤回")], default="draft", max_length=16)),
                ("origin", models.CharField(choices=[("automatic", "自动生成"), ("manual", "手动创建"), ("initial", "首期专题")], default="manual", max_length=16)),
                ("current_revision", models.PositiveIntegerField(default=0)),
                ("published_revision", models.PositiveIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_management_insights", to=settings.AUTH_USER_MODEL)),
                ("published_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="published_management_insights", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-published_at", "-updated_at"]},
        ),
        migrations.CreateModel(
            name="ManagementInsightRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("revision", models.PositiveIntegerField()),
                ("title", models.CharField(max_length=140)),
                ("summary", models.TextField()),
                ("facts", models.JSONField(default=list)),
                ("mechanisms", models.JSONField(default=list)),
                ("associations", models.JSONField(default=list)),
                ("hypotheses", models.JSONField(default=list)),
                ("actions", models.JSONField(default=list)),
                ("metric_snapshot", models.JSONField(default=dict)),
                ("source_hashes", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="management_insight_revisions", to=settings.AUTH_USER_MODEL)),
                ("insight", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="revisions", to="dashboard.managementinsight")),
            ],
            options={"ordering": ["-revision"]},
        ),
        migrations.AddConstraint(
            model_name="managementinsightrevision",
            constraint=models.UniqueConstraint(fields=("insight", "revision"), name="unique_management_insight_revision"),
        ),
    ]

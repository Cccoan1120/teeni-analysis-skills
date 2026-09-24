from django.db import migrations, models


def _scrub_fingerprints(value):
    if isinstance(value, dict):
        return {
            key: _scrub_fingerprints(item)
            for key, item in value.items()
            if "fingerprint" not in str(key).casefold()
        }
    if isinstance(value, list):
        return [_scrub_fingerprints(item) for item in value]
    return value


def remove_approval_data(apps, _schema_editor):
    TopicJob = apps.get_model("topics", "TopicJob")
    TopicAuditEvent = apps.get_model("topics", "TopicAuditEvent")
    TopicSnapshot = apps.get_model("topics", "TopicSnapshot")

    TopicJob.objects.filter(status="awaiting_approval").update(status="queued", progress=45)
    for event in TopicAuditEvent.objects.iterator():
        cleaned = _scrub_fingerprints(event.details)
        if cleaned != event.details:
            event.details = cleaned
            event.save(update_fields=["details"])
    for snapshot in TopicSnapshot.objects.iterator():
        payload = _scrub_fingerprints(snapshot.payload)
        method = dict(payload.get("method") or {})
        method.setdefault("authorizationMode", "manual_explicit")
        payload["method"] = method
        payload["schemaVersion"] = "teeni-topic-snapshot/2.0.0"
        snapshot.payload = payload
        snapshot.save(update_fields=["payload"])


class Migration(migrations.Migration):
    dependencies = [("topics", "0002_topicjob_next_attempt_at_topicjob_runtime_state")]

    operations = [
        migrations.RunPython(remove_approval_data, migrations.RunPython.noop),
        migrations.RemoveField(model_name="topicjob", name="approved_at"),
        migrations.RemoveField(model_name="topicjob", name="approved_by"),
        migrations.RemoveField(model_name="topicjob", name="approval_fingerprint"),
        migrations.RemoveField(model_name="topicjob", name="preflight"),
        migrations.AlterField(
            model_name="topicjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("uploading", "上传中"),
                    ("validating", "本地校验"),
                    ("base_running", "基础分析"),
                    ("base_verifying", "基础验证"),
                    ("queued", "排队"),
                    ("semantic_running", "语义分析"),
                    ("final_verifying", "最终验证"),
                    ("publishing", "发布"),
                    ("completed", "发布完成"),
                    ("waiting_model", "等待模型"),
                    ("failed", "失败"),
                    ("expired", "已过期"),
                ],
                default="uploading",
                max_length=32,
            ),
        ),
    ]

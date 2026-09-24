import shutil
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import TopicAuditEvent, TopicJob
from .storage import job_directory, storage_root


def cleanup_expired_interest_batches(*, now=None) -> int:
    return _cleanup_expired_directories("interest-batches", now=now)


def cleanup_expired_interest_cache(*, now=None) -> int:
    return _cleanup_expired_directories("interest-daily-cache", now=now)


def _cleanup_expired_directories(folder, *, now=None) -> int:
    cutoff = (now or timezone.now()) - timedelta(days=settings.TEENI_TOPIC_RETENTION_DAYS)
    batch_root = (storage_root() / folder).resolve()
    if not batch_root.is_dir():
        return 0
    cleaned = 0
    for directory in batch_root.iterdir():
        resolved = directory.resolve()
        if not directory.is_dir() or resolved.parent != batch_root:
            continue
        if directory.stat().st_mtime > cutoff.timestamp():
            continue
        shutil.rmtree(resolved)
        cleaned += 1
    return cleaned


def cleanup_expired_jobs(*, now=None) -> int:
    cutoff = now or timezone.now()
    job_ids = list(
        TopicJob.objects.filter(expires_at__lte=cutoff)
        .exclude(status=TopicJob.Status.EXPIRED)
        .values_list("id", flat=True)
    )
    cleaned = 0
    for job_id in job_ids:
        directory = job_directory(job_id)
        if directory.is_dir():
            shutil.rmtree(directory)
        with transaction.atomic():
            job = TopicJob.objects.select_for_update().get(id=job_id)
            if job.expires_at > cutoff or job.status == TopicJob.Status.EXPIRED:
                continue
            job.artifacts.all().delete()
            job.status = TopicJob.Status.EXPIRED
            job.save(update_fields=["status", "updated_at"])
            TopicAuditEvent.objects.create(job=job, action="job_files_expired", details={})
            cleaned += 1
    return cleaned

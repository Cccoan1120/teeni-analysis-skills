import tempfile
import os
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from topics.cleanup import cleanup_expired_interest_batches, cleanup_expired_interest_cache, cleanup_expired_jobs
from topics.models import TopicArtifact, TopicJob, TopicSnapshot
from topics.storage import job_directory, safe_job_path


class TopicCleanupTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = override_settings(TEENI_TOPIC_STORAGE_ROOT=Path(self.temp.name))
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        self.user = get_user_model().objects.create_user("cleanup@example.com", password="a-strong-password-42")

    def test_expired_files_and_artifacts_are_removed_but_snapshot_remains(self):
        job = TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-20",
            original_name="scene488.csv",
            expected_size=1,
            status=TopicJob.Status.COMPLETED,
            input_sha256="a" * 64,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        path = safe_job_path(job.id, "artifacts/report.xlsx", create_parent=True)
        path.write_bytes(b"report")
        TopicArtifact.objects.create(
            job=job,
            kind=TopicArtifact.Kind.TOPIC_WORKBOOK,
            relative_path="artifacts/report.xlsx",
            download_name="report.xlsx",
            sha256="b" * 64,
            byte_size=6,
            verified=True,
            expires_at=job.expires_at,
        )
        TopicSnapshot.objects.create(
            data_date=job.data_date,
            job=job,
            taxonomy_version="6.0.0",
            catalog_revision="2026-08-17.1",
            report_schema="teeni-topic-report/3.1.0",
            source_sha256="a" * 64,
            workbook_sha256="b" * 64,
            payload={"aggregate": True},
        )
        self.assertEqual(cleanup_expired_jobs(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, TopicJob.Status.EXPIRED)
        self.assertFalse(job_directory(job.id).exists())
        self.assertFalse(TopicArtifact.objects.filter(job=job).exists())
        self.assertTrue(TopicSnapshot.objects.filter(job=job).exists())

    def test_expired_interest_batch_private_files_are_removed(self):
        batch_root = Path(self.temp.name) / "interest-batches"
        old_batch = batch_root / "old-batch"
        current_batch = batch_root / "current-batch"
        old_batch.mkdir(parents=True)
        current_batch.mkdir()
        (old_batch / "teeni-interest-detail.csv").write_text("private", encoding="utf-8")
        (current_batch / "teeni-interest-detail.csv").write_text("private", encoding="utf-8")
        now = timezone.now()
        old_time = (now - timedelta(days=31)).timestamp()
        os.utime(old_batch, (old_time, old_time))

        self.assertEqual(cleanup_expired_interest_batches(now=now), 1)
        self.assertFalse(old_batch.exists())
        self.assertTrue(current_batch.exists())

    def test_daily_cache_expires_without_deleting_cumulative_contributions(self):
        from interests.models import InterestDailyContribution
        contribution = InterestDailyContribution.objects.create(data_date="2026-08-20", source_sha256="a" * 64,
            registry_sha256="b" * 64, identity_sha256="c" * 64, detail_sha256="d" * 64, payload=b"private-contribution")
        cache = Path(self.temp.name) / "interest-daily-cache"
        old, current = cache / "old", cache / "current"
        old.mkdir(parents=True)
        current.mkdir()
        (old / "teeni-interest-detail.csv").write_text("private")
        now = timezone.now()
        timestamp = (now - timedelta(days=31)).timestamp()
        os.utime(old, (timestamp, timestamp))
        self.assertEqual(cleanup_expired_interest_cache(now=now), 1)
        self.assertFalse(old.exists())
        self.assertTrue(current.exists())
        self.assertTrue(InterestDailyContribution.objects.filter(pk=contribution.pk).exists())

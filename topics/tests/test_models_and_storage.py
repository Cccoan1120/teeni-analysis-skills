import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.test import TestCase, override_settings

from topics.models import TopicArtifact, TopicJob
from topics.storage import disk_has_capacity, safe_job_path, upload_path


class TopicStorageTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = override_settings(TEENI_TOPIC_STORAGE_ROOT=Path(self.temp.name))
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        self.user = get_user_model().objects.create_user("analyst@example.com", password="a-strong-password-42")
        self.job = TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-20",
            original_name="scene488.csv",
            expected_size=100,
        )

    def test_upload_path_is_job_scoped(self):
        path = upload_path(self.job.id, create=True)
        self.assertEqual(path.parent.name, "input")
        self.assertTrue(str(path).startswith(self.temp.name))

    def test_unsafe_relative_path_is_rejected(self):
        with self.assertRaises(SuspiciousFileOperation):
            safe_job_path(self.job.id, "../other.csv")

    def test_artifact_rejects_unsafe_path(self):
        artifact = TopicArtifact(
            job=self.job,
            kind=TopicArtifact.Kind.TOPIC_WORKBOOK,
            relative_path="../report.xlsx",
            download_name="report.xlsx",
            sha256="a" * 64,
            byte_size=1,
        )
        with self.assertRaises(ValidationError):
            artifact.full_clean()

    @patch("topics.storage.shutil.disk_usage")
    def test_disk_reserve_is_enforced(self, usage):
        usage.return_value = type("Usage", (), {"total": 1000, "free": 250})()
        self.assertFalse(disk_has_capacity(100))
        self.assertTrue(disk_has_capacity(40))

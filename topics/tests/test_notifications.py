import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from topics.models import TopicJob
from topics.notifications import build_notification_payload, notify_job


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class NotificationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("notify@example.com", password="a-strong-password-42")
        self.job = TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-20",
            original_name="private-file-name.csv",
            expected_size=1,
            status=TopicJob.Status.WAITING_MODEL,
            error_message="private raw text",
        )
        self.job.refresh_from_db()

    @override_settings(TEENI_PUBLIC_BASE_URL="https://dashboard.example.com")
    def test_payload_contains_only_date_status_and_dashboard_link(self):
        payload = build_notification_payload(self.job)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("2026-08-20", encoded)
        self.assertIn("等待模型", encoded)
        self.assertIn("https://dashboard.example.com/?view=interest-jobs", encoded)
        self.assertNotIn(self.job.original_name, encoded)
        self.assertNotIn(self.job.error_message, encoded)
        self.assertNotIn(str(self.job.id), encoded)

    def test_webhook_is_read_from_credential_file(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "webhook"
            secret.write_text("https://open.feishu.cn/open-apis/bot/v2/hook/test", encoding="utf-8")
            secret.chmod(0o600)
            captured = []

            def opener(request, timeout):
                captured.append((request.full_url, timeout, request.data))
                return FakeResponse()

            with override_settings(
                TEENI_FEISHU_WEBHOOK_FILE=str(secret),
                TEENI_PUBLIC_BASE_URL="https://dashboard.example.com",
            ):
                self.assertTrue(notify_job(self.job, opener=opener))
            self.assertEqual(captured[0][0], secret.read_text(encoding="utf-8"))
            self.assertNotIn(captured[0][0], captured[0][2].decode("utf-8"))

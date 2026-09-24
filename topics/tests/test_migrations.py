import json
from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class RemoveTopicApprovalMigrationTests(TransactionTestCase):
    migrate_from = [("topics", "0002_topicjob_next_attempt_at_topicjob_runtime_state")]
    migrate_to = [("topics", "0003_remove_topic_approval")]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        old_apps = self.executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model("auth", "User")
        TopicJob = old_apps.get_model("topics", "TopicJob")
        TopicAuditEvent = old_apps.get_model("topics", "TopicAuditEvent")
        TopicSnapshot = old_apps.get_model("topics", "TopicSnapshot")

        user = User.objects.create(username="migration@example.com", email="migration@example.com")
        job = TopicJob.objects.create(
            created_by=user,
            data_date=date(2026, 8, 20),
            original_name="scene488.csv",
            expected_size=1,
            status="awaiting_approval",
            progress=40,
            preflight={"approvalFingerprint": "a" * 64},
            approval_fingerprint="a" * 64,
        )
        TopicAuditEvent.objects.create(
            job=job,
            action="semantic_preflight_ready",
            details={"approvalFingerprint": "a" * 64, "nested": [{"fingerprint": "b" * 64}]},
        )
        TopicSnapshot.objects.create(
            data_date=job.data_date,
            job=job,
            taxonomy_version="6.0.0",
            catalog_revision="2026-08-17.1",
            report_schema="teeni-topic-report/2.0.0",
            source_sha256="c" * 64,
            workbook_sha256="d" * 64,
            payload={
                "schemaVersion": "teeni-topic-snapshot/1.0.0",
                "method": {"approvalFingerprint": "a" * 64},
            },
        )
        self.job_id = job.id

        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        self.apps = self.executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        self.executor.loader.build_graph()
        self.executor.migrate(self.executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_waiting_job_is_queued_and_public_json_is_scrubbed(self):
        TopicJob = self.apps.get_model("topics", "TopicJob")
        TopicAuditEvent = self.apps.get_model("topics", "TopicAuditEvent")
        TopicSnapshot = self.apps.get_model("topics", "TopicSnapshot")

        job = TopicJob.objects.get(id=self.job_id)
        event = TopicAuditEvent.objects.get(job_id=self.job_id)
        snapshot = TopicSnapshot.objects.get(job_id=self.job_id)
        self.assertEqual((job.status, job.progress), ("queued", 45))
        self.assertNotIn("fingerprint", json.dumps(event.details).casefold())
        self.assertNotIn("fingerprint", json.dumps(snapshot.payload).casefold())
        self.assertEqual(snapshot.payload["schemaVersion"], "teeni-topic-snapshot/2.0.0")
        self.assertEqual(snapshot.payload["method"]["authorizationMode"], "manual_explicit")

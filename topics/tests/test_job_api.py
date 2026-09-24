import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from interest_engine import ENGINE_VERSION
from topics.models import TopicArtifact, TopicJob, TopicSnapshot
from topics.storage import job_directory, safe_job_path


@override_settings(
    TEENI_TOPIC_CHUNK_BYTES=8,
    TEENI_TOPIC_MAX_UPLOAD_BYTES=1000,
    TEENI_TOPIC_PIPELINE_MODE="interest_v1",
)
class TopicJobApiTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = override_settings(TEENI_TOPIC_STORAGE_ROOT=Path(self.temp.name))
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        self.user = get_user_model().objects.create_user("analyst@example.com", password="a-strong-password-42")
        self.client.force_login(self.user)

    def create_job(self, *, size=12, pipeline=None):
        payload = {"dataDate": "2026-08-20", "fileName": "scene488.csv", "fileSize": size}
        if pipeline is not None:
            payload["pipeline"] = pipeline
        with patch("topics.api.disk_has_capacity", return_value=True):
            return self.client.post(
                reverse("topics:create-job"),
                data=json.dumps(payload),
                content_type="application/json",
            )

    def test_m2_upload_is_explicit_and_job_lists_are_separate(self):
        first = self.create_job().json()["job"]
        with patch("topics.api.disk_has_capacity", return_value=True):
            response = self.client.post("/api/interest-jobs", data=json.dumps({
                "productVersion": "M2", "dataDate": "2026-08-20", "fileName": "scene488.csv", "fileSize": 12,
            }), content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        second = response.json()["job"]
        self.assertEqual((second["productVersion"], second["sceneId"]), ("M2", "904"))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual([row["id"] for row in self.client.get("/api/interest-jobs").json()["items"]], [first["id"]])
        self.assertEqual([row["id"] for row in self.client.get("/api/interest-jobs?productVersion=M2").json()["items"]], [second["id"]])
        self.assertEqual(self.client.get(f"/api/interest-jobs/{second['id']}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/interest-jobs/{second['id']}?productVersion=M2").status_code, 200)
        self.assertEqual(self.client.get("/api/interest-jobs?productVersion=M3").status_code, 400)

    def test_job_api_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("topics:list-jobs"))
        self.assertEqual(response.status_code, 302)

    def test_fixed_job_collection_path_supports_get_and_post(self):
        listing = self.client.get("/api/topic-jobs")
        created = self.create_job()
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(created.status_code, 201)

    def test_new_job_freezes_only_interest_runtime(self):
        response = self.create_job()
        job = TopicJob.objects.get(id=response.json()["job"]["id"])

        self.assertNotIn("topicRuntime", job.runtime_state)
        self.assertEqual(job.pipeline, TopicJob.Pipeline.INTEREST_V1)
        self.assertEqual(job.runtime_state["interestEngine"], ENGINE_VERSION)

    @override_settings(TEENI_TOPIC_PIPELINE_MODE="legacy_topic")
    def test_explicit_interest_pipeline_overrides_server_default(self):
        response = self.create_job(pipeline="interest_v1")

        self.assertEqual(response.status_code, 201)
        job = TopicJob.objects.get(id=response.json()["job"]["id"])
        self.assertEqual(job.pipeline, TopicJob.Pipeline.INTEREST_V1)
        self.assertEqual(job.runtime_state["interestEngine"], ENGINE_VERSION)

    def test_legacy_pipeline_is_rejected(self):
        response = self.create_job(pipeline="legacy_topic")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_pipeline")
        self.assertFalse(TopicJob.objects.exists())

    def test_upload_cannot_select_internal_or_unknown_pipeline(self):
        shadow = self.create_job(pipeline="shadow")
        unknown = self.create_job(pipeline="unknown")

        self.assertEqual(shadow.status_code, 400)
        self.assertEqual(shadow.json()["error"], "invalid_pipeline")
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()["error"], "invalid_pipeline")
        self.assertFalse(TopicJob.objects.exists())

    @override_settings(TEENI_TOPIC_PIPELINE_MODE="shadow")
    def test_old_shadow_setting_cannot_enable_retired_pipeline(self):
        response = self.create_job()
        job = TopicJob.objects.get(id=response.json()["job"]["id"])

        self.assertEqual(job.pipeline, TopicJob.Pipeline.INTEREST_V1)
        self.assertNotIn("topicRuntime", job.runtime_state)
        self.assertEqual(job.runtime_state["interestEngine"], ENGINE_VERSION)

    @override_settings(TEENI_TOPIC_PIPELINE_MODE="legacy_topic")
    def test_old_legacy_setting_cannot_enable_retired_pipeline(self):
        response = self.create_job()
        job = TopicJob.objects.get(id=response.json()["job"]["id"])

        self.assertEqual(job.pipeline, TopicJob.Pipeline.INTEREST_V1)
        self.assertNotIn("topicRuntime", job.runtime_state)

    def test_storage_failure_does_not_leave_resumable_orphan(self):
        with patch("topics.api.disk_has_capacity", return_value=True), patch(
            "topics.api.upload_path", side_effect=PermissionError("denied")
        ):
            response = self.client.post(
                reverse("topics:create-job"),
                data=json.dumps(
                    {"dataDate": "2026-08-20", "fileName": "scene488.csv", "fileSize": 12}
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "storage_unavailable")
        self.assertFalse(TopicJob.objects.exists())

    def test_chunk_upload_resumes_at_server_offset(self):
        job_id = self.create_job().json()["job"]["id"]
        url = reverse("topics:upload-chunk", args=[job_id])
        first = self.client.patch(url, data=b"abcdefgh", content_type="application/octet-stream", HTTP_UPLOAD_OFFSET="0")
        mismatch = self.client.patch(url, data=b"ijkl", content_type="application/octet-stream", HTTP_UPLOAD_OFFSET="0")
        second = self.client.patch(url, data=b"ijkl", content_type="application/octet-stream", HTTP_UPLOAD_OFFSET="8")
        self.assertEqual(first.status_code, 204)
        self.assertEqual(first.headers["Upload-Offset"], "8")
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.headers["Upload-Offset"], "8")
        self.assertEqual(second.headers["Upload-Offset"], "12")

    def test_oversized_chunk_and_overflow_are_rejected(self):
        job_id = self.create_job(size=9).json()["job"]["id"]
        url = reverse("topics:upload-chunk", args=[job_id])
        oversized = self.client.patch(url, data=b"123456789", content_type="application/octet-stream", HTTP_UPLOAD_OFFSET="0")
        self.client.patch(url, data=b"12345678", content_type="application/octet-stream", HTTP_UPLOAD_OFFSET="0")
        overflow = self.client.patch(url, data=b"12", content_type="application/octet-stream", HTTP_UPLOAD_OFFSET="8")
        self.assertEqual(oversized.status_code, 400)
        self.assertEqual(overflow.status_code, 409)

    def test_complete_upload_requires_exact_size_and_sha(self):
        job_id = self.create_job(size=4).json()["job"]["id"]
        complete_url = reverse("topics:complete-upload", args=[job_id])
        incomplete = self.client.post(complete_url, data=json.dumps({"sha256": "a" * 64}), content_type="application/json")
        self.assertEqual(incomplete.status_code, 409)
        self.client.patch(
            reverse("topics:upload-chunk", args=[job_id]),
            data=b"data",
            content_type="application/octet-stream",
            HTTP_UPLOAD_OFFSET="0",
        )
        invalid = self.client.post(complete_url, data=json.dumps({"sha256": "bad"}), content_type="application/json")
        completed = self.client.post(
            complete_url,
            data=json.dumps({"sha256": hashlib.sha256(b"data").hexdigest()}),
            content_type="application/json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(completed.status_code, 202)
        self.assertEqual(TopicJob.objects.get(id=job_id).status, TopicJob.Status.VALIDATING)

    def test_detail_does_not_expose_storage_paths(self):
        job = TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-20",
            original_name="scene488.csv",
            expected_size=4,
            received_size=4,
            status=TopicJob.Status.COMPLETED,
        )
        path = safe_job_path(job.id, "outputs/report.xlsx", create_parent=True)
        path.write_bytes(b"xlsx")
        artifact = TopicArtifact.objects.create(
            job=job,
            kind=TopicArtifact.Kind.INTEREST_WORKBOOK,
            relative_path="outputs/report.xlsx",
            download_name="Teeni-topic.xlsx",
            sha256=hashlib.sha256(b"xlsx").hexdigest(),
            byte_size=4,
            verified=True,
        )
        response = self.client.get(reverse("topics:job-detail", args=[job.id]))
        body = response.content.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.temp.name, body)
        download = self.client.get(reverse("topics:download-artifact", args=[job.id, artifact.id]))
        self.assertEqual(download.status_code, 200)
        download.close()

    def test_approval_endpoint_is_removed_and_payload_has_no_approval_fields(self):
        job = TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-20",
            original_name="scene488.csv",
            expected_size=4,
            status=TopicJob.Status.QUEUED,
        )
        removed = self.client.post(
            f"/api/topic-jobs/{job.id}/approve",
            data=json.dumps({"fingerprint": "a" * 64}),
            content_type="application/json",
        )
        payload = self.client.get(reverse("topics:job-detail", args=[job.id])).json()["job"]
        self.assertEqual(removed.status_code, 404)
        for field in ("preflight", "approved", "approvedAt", "approvalFingerprint"):
            self.assertNotIn(field, payload)

    def test_new_upload_is_blocked_below_disk_reserve(self):
        with patch("topics.api.disk_has_capacity", return_value=False):
            response = self.client.post(
                reverse("topics:create-job"),
                data=json.dumps({"dataDate": "2026-08-20", "fileName": "scene488.csv", "fileSize": 12}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 507)

    def test_delete_requires_login(self):
        job = TopicJob.objects.create(
            created_by=self.user, data_date="2026-08-20", original_name="scene488.csv",
            expected_size=4, status=TopicJob.Status.WAITING_MODEL,
        )
        self.client.logout()
        response = self.client.delete(reverse("topics:job-detail", args=[job.id]))
        self.assertEqual(response.status_code, 302)

    def test_waiting_job_delete_removes_files_and_hides_job(self):
        job = TopicJob.objects.create(
            created_by=self.user, data_date="2026-08-20", original_name="scene488.csv",
            expected_size=4, status=TopicJob.Status.WAITING_MODEL,
            runtime_state={"semanticFailure": {"failureCodes": {"http_400": 1}}},
        )
        safe_job_path(job.id, "topic/report.xlsx", create_parent=True).write_bytes(b"xlsx")
        response = self.client.delete(reverse("topics:job-detail", args=[job.id]))
        self.assertEqual(response.status_code, 204)
        job.refresh_from_db()
        self.assertEqual(job.status, TopicJob.Status.EXPIRED)
        self.assertEqual(job.error_code, "user_deleted")
        self.assertEqual(job.runtime_state, {})
        self.assertFalse(job_directory(job.id).exists())
        self.assertNotIn(str(job.id), [item["id"] for item in self.client.get(reverse("topics:list-jobs")).json()["items"]])
        self.assertEqual(self.client.get(reverse("topics:job-detail", args=[job.id])).status_code, 404)

    def test_active_job_delete_is_rejected_and_files_remain(self):
        job = TopicJob.objects.create(
            created_by=self.user, data_date="2026-08-20", original_name="scene488.csv",
            expected_size=4, status=TopicJob.Status.SEMANTIC_RUNNING,
        )
        file_path = safe_job_path(job.id, "topic/progress.json", create_parent=True)
        file_path.write_text("{}", encoding="utf-8")
        response = self.client.delete(reverse("topics:job-detail", args=[job.id]))
        self.assertEqual(response.status_code, 409)
        job.refresh_from_db()
        self.assertEqual(job.status, TopicJob.Status.SEMANTIC_RUNNING)
        self.assertTrue(file_path.is_file())

    def test_retry_interest_failure_freezes_registry_before_analysis(self):
        job = TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-20",
            original_name="scene488.csv",
            expected_size=4,
            pipeline=TopicJob.Pipeline.INTEREST_V1,
            status=TopicJob.Status.FAILED,
            error_code="analysis_timeout",
            runtime_state={"base": {"manifestPath": "base/manifest.json"}},
        )

        response = self.client.post(reverse("topics:retry-job", args=[job.id]), data="{}", content_type="application/json")

        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, TopicJob.Status.BASE_VERIFYING)

    def test_completed_job_delete_preserves_snapshot_and_removes_artifact(self):
        job = TopicJob.objects.create(
            created_by=self.user, data_date="2026-08-20", original_name="scene488.csv",
            expected_size=4, status=TopicJob.Status.COMPLETED,
        )
        artifact_path = safe_job_path(job.id, "topic/report.xlsx", create_parent=True)
        artifact_path.write_bytes(b"xlsx")
        artifact = TopicArtifact.objects.create(
            job=job, kind=TopicArtifact.Kind.TOPIC_WORKBOOK,
            relative_path="topic/report.xlsx", download_name="report.xlsx",
            sha256=hashlib.sha256(b"xlsx").hexdigest(), byte_size=4, verified=True,
        )
        snapshot = TopicSnapshot.objects.create(
            data_date=job.data_date, job=job, taxonomy_version="6.0.0",
            catalog_revision="2026-08-17.1", report_schema="teeni-topic-report/3.1.0",
            source_sha256="a" * 64, workbook_sha256="b" * 64, payload={"aggregate": True},
        )
        download_url = reverse("topics:download-artifact", args=[job.id, artifact.id])
        response = self.client.delete(reverse("topics:job-detail", args=[job.id]))
        self.assertEqual(response.status_code, 204)
        self.assertTrue(TopicSnapshot.objects.filter(id=snapshot.id, job=job).exists())
        self.assertFalse(TopicArtifact.objects.filter(id=artifact.id).exists())
        self.assertEqual(self.client.get(download_url).status_code, 404)


    def test_legacy_jobs_are_archived_on_both_api_routes(self):
        for pipeline in (TopicJob.Pipeline.LEGACY_TOPIC, TopicJob.Pipeline.SHADOW):
            job = TopicJob.objects.create(
                created_by=self.user, data_date="2026-08-20", original_name="old.csv",
                expected_size=1, pipeline=pipeline, status=TopicJob.Status.WAITING_MODEL,
            )
            for prefix in ("/api/interest-jobs", "/api/topic-jobs"):
                self.assertEqual(self.client.get(prefix).json()["items"], [])
                self.assertEqual(self.client.get(f"{prefix}/{job.id}").status_code, 404)
                self.assertEqual(self.client.post(f"{prefix}/{job.id}/retry").status_code, 404)
                self.assertEqual(self.client.delete(f"{prefix}/{job.id}").status_code, 404)
            self.assertTrue(TopicJob.objects.filter(id=job.id).exists())
        self.assertEqual(self.client.get("/api/topic-snapshots/").status_code, 404)

    def test_publish_retry_uses_verified_artifacts(self):
        job = TopicJob.objects.create(
            created_by=self.user, data_date="2026-08-20", original_name="data.csv",
            expected_size=1, status=TopicJob.Status.FAILED, error_code="publish_failed",
            runtime_state={"base": {}, "interest": {}, "interestVerified": True},
        )
        response = self.client.post(reverse("topics:retry-job", args=[job.id]))
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, TopicJob.Status.PUBLISHING)

    def test_archived_upload_and_download_cannot_be_resumed(self):
        job = TopicJob.objects.create(
            created_by=self.user, data_date="2026-08-20", original_name="old.csv",
            expected_size=1, pipeline=TopicJob.Pipeline.LEGACY_TOPIC,
        )
        artifact = TopicArtifact.objects.create(
            job=job, kind=TopicArtifact.Kind.TOPIC_WORKBOOK,
            relative_path="old.xlsx", download_name="old.xlsx", sha256="a" * 64,
            byte_size=1, verified=True,
        )
        for prefix in ("/api/interest-jobs", "/api/topic-jobs"):
            self.assertEqual(self.client.patch(
                f"{prefix}/{job.id}/upload", data=b"x", content_type="application/octet-stream",
                HTTP_UPLOAD_OFFSET="0",
            ).status_code, 404)
            self.assertEqual(self.client.post(
                f"{prefix}/{job.id}/complete-upload", data=json.dumps({"sha256": "a" * 64}),
                content_type="application/json",
            ).status_code, 404)
            self.assertEqual(self.client.get(f"{prefix}/{job.id}/events").status_code, 404)
            self.assertEqual(self.client.get(f"{prefix}/{job.id}/artifacts/{artifact.id}").status_code, 404)
        self.assertTrue(TopicArtifact.objects.filter(id=artifact.id).exists())


class TopicChunkBodyLimitTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = override_settings(TEENI_TOPIC_STORAGE_ROOT=Path(self.temp.name))
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        self.user = get_user_model().objects.create_user("analyst@example.com", password="a-strong-password-42")
        self.client.force_login(self.user)

    def test_eight_mib_chunk_reaches_upload_view(self):
        chunk = b"x" * settings.TEENI_TOPIC_CHUNK_BYTES
        with patch("topics.api.disk_has_capacity", return_value=True):
            created = self.client.post(
                reverse("topics:create-job"),
                data=json.dumps(
                    {
                        "dataDate": "2026-08-20",
                        "fileName": "scene488.csv",
                        "fileSize": len(chunk),
                    }
                ),
                content_type="application/json",
            )
        job_id = created.json()["job"]["id"]

        response = self.client.patch(
            reverse("topics:upload-chunk", args=[job_id]),
            data=chunk,
            content_type="application/octet-stream",
            HTTP_UPLOAD_OFFSET="0",
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Upload-Offset"], str(len(chunk)))

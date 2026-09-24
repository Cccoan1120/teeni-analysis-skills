import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from interest_engine import ENGINE_VERSION
from topics.models import TopicJob
from topics.runtime import TopicConfigurationError
from topics.worker import claim_next_job, process_backfill_request, process_job


class FakeRuntime:
    def __init__(self):
        self.calls = []

    def validate_upload(self, _job):
        self.calls.append("validate")
        return {"inputPath": "input/scene488.csv", "inputSha256": "b" * 64, "rowCount": 10}

    def run_base(self, _job):
        self.calls.append("base")
        return {"workbookPath": "base/report.xlsx", "primaryDetailPath": "base/detail.csv", "endingDetailPath": "base/end.csv", "manifestPath": "base/manifest.json"}

    def verify_base(self, _job):
        self.calls.append("verify_base")

    def freeze_interest(self, _job):
        self.calls.append("freeze_interest")
        return {"engineVersion": ENGINE_VERSION, "registrySha256": "c" * 64}

    def run_interest(self, _job):
        self.calls.append("interest")
        return {
            "candidateDegraded": True,
            "candidateFailureCode": "candidate_model_unavailable",
            "inputQueries": 10,
        }

    def verify_interest(self, _job):
        self.calls.append("verify_interest")


class TopicWorkerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("worker@example.com", password="a-strong-password-42")

    def job(self, status, **values):
        defaults = {
            "created_by": self.user,
            "data_date": "2026-08-20",
            "original_name": "scene488.csv",
            "expected_size": 1,
            "status": status,
        }
        defaults.update(values)
        return TopicJob.objects.create(**defaults)

    def test_invalid_publish_fails_without_replacing_state(self):
        job = self.job(TopicJob.Status.PUBLISHING)

        def invalid_publish(_job):
            raise ValueError("invalid aggregate")

        result = process_job(job.id, runtime=FakeRuntime(), publisher=invalid_publish)
        result.refresh_from_db()
        self.assertEqual(result.status, TopicJob.Status.FAILED)
        self.assertEqual(result.error_code, "publish_failed")

    def test_interest_pipeline_skips_model_gate_and_completes_when_candidates_degrade(self):
        job = self.job(TopicJob.Status.VALIDATING, pipeline=TopicJob.Pipeline.INTEREST_V1)
        runtime = FakeRuntime()
        published = []
        result = process_job(job.id, runtime=runtime, publisher=lambda item: published.append(item.id))
        result.refresh_from_db()
        self.assertEqual(result.status, TopicJob.Status.COMPLETED)
        self.assertEqual(
            runtime.calls,
            ["validate", "base", "verify_base", "freeze_interest", "interest", "verify_interest"],
        )
        self.assertEqual(published, [job.id])
        self.assertTrue(result.runtime_state["interest"]["candidateDegraded"])


    def test_approved_entity_backfill_republishes_retained_interest_snapshots_without_model(self):
        from interests.models import InterestBackfillRequest, InterestEntity, InterestSnapshot

        entity = InterestEntity.objects.create(
            canonical_name="野生狗奶",
            entity_type="网络热梗",
            broad_topic="其他明确内容",
        )
        job = self.job(
            TopicJob.Status.COMPLETED,
            pipeline=TopicJob.Pipeline.INTEREST_V1,
            runtime_state={"base": {"primaryDetailPath": "base/detail.csv", "manifestPath": "base/manifest.json"}},
        )
        InterestSnapshot.objects.create(
            data_date=job.data_date,
            job=job,
            schema_version="teeni-interest-snapshot/1.1.0",
            engine_version="teeni-interest-engine/1.1.0",
            source_sha256="a" * 64,
            registry_sha256="b" * 64,
            workbook_sha256="c" * 64,
            payload={"entities": []},
        )
        request = InterestBackfillRequest.objects.create(
            entity=entity,
            start_date="2026-08-13",
            end_date="2026-08-20",
            requested_by=self.user,
        )

        class BackfillRuntime:
            def __init__(self):
                self.calls = []

            def run_interest_backfill(self, source_job, **kwargs):
                self.calls.append((source_job.id, kwargs))
                registry = json.loads(kwargs["registry_path"].read_text(encoding="utf-8"))
                self_entity = next(item for item in registry["entities"] if item["canonicalName"] == "野生狗奶")
                return ({"manifestPath": "backfill/manifest.json"}, {
                    "registryPath": kwargs["registry_path"].relative_to(kwargs["registry_path"].parents[2]).as_posix(),
                    "registrySha256": kwargs["registry_sha256"],
                })

        runtime = BackfillRuntime()
        published = []

        def publisher(source_job, **kwargs):
            published.append((source_job.id, kwargs))

        result = process_backfill_request(request.id, runtime=runtime, publisher=publisher)
        result.refresh_from_db()
        self.assertEqual(result.status, InterestBackfillRequest.Status.COMPLETED)
        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(len(published), 1)
        self.assertFalse(published[0][1]["require_publishing"])
        self.assertFalse(published[0][1]["replace_candidates"])
        self.assertNotIn("model", json.dumps(runtime.calls[0][1], default=str).casefold())
        entity.refresh_from_db()
        self.assertEqual(job.events.get(action="interest_entity_backfilled").details["entityRegistryId"], entity.registry_id)

    def test_worker_never_claims_or_executes_archived_pipelines(self):
        for pipeline in (TopicJob.Pipeline.LEGACY_TOPIC, TopicJob.Pipeline.SHADOW):
            job = self.job(TopicJob.Status.VALIDATING, pipeline=pipeline)
            runtime = FakeRuntime()
            with self.assertRaises(TopicConfigurationError):
                process_job(job.id, runtime=runtime, publisher=lambda job: self.fail("unexpected publish"))
            self.assertEqual(runtime.calls, [])
            job.refresh_from_db()
            self.assertEqual(job.status, TopicJob.Status.VALIDATING)
        self.assertIsNone(claim_next_job())
        interest = self.job(TopicJob.Status.VALIDATING)
        self.assertEqual(claim_next_job().id, interest.id)

    def test_candidate_review_packages_are_never_claimed_as_uploads(self):
        job = self.job(TopicJob.Status.INTEREST_VERIFYING, runtime_state={"interestCandidateStaging": {"productVersion": "M2"}}, product_version="M2")
        self.assertIsNone(claim_next_job())
        with self.assertRaises(TopicConfigurationError):
            process_job(job.id, runtime=FakeRuntime())

    def test_publishing_resume_does_not_repeat_analysis(self):
        job = self.job(TopicJob.Status.PUBLISHING, runtime_state={"interestVerified": True})
        runtime = FakeRuntime()
        published = []
        result = process_job(job.id, runtime=runtime, publisher=lambda job: published.append(job.id))
        self.assertEqual(result.status, TopicJob.Status.COMPLETED)
        self.assertEqual(runtime.calls, [])
        self.assertEqual(published, [job.id])

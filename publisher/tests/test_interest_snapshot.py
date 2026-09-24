import csv
import hashlib
import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from interest_engine.analysis import analyze
from interest_engine.model import LocalCandidatePassthroughEnricher
from interests.models import InterestCandidate, InterestDailyContribution, InterestSegmentSnapshot, InterestSnapshot
from interests.registry import freeze_registry, review_candidate
from publisher.interest_snapshot import InterestSnapshotContractError, publish_interest_job, validate_interest_payload
from topics.models import TopicArtifact, TopicJob, TopicSnapshot
from topics.storage import job_directory, safe_job_path


class InterestSnapshotTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = override_settings(TEENI_TOPIC_STORAGE_ROOT=Path(self.temp.name) / "storage")
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        self.user = get_user_model().objects.create_user("interest-publisher@example.com", password="a-strong-password-42")

    def _interest_job(self, enricher=None):
        job = TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-25",
            original_name="scene488.csv",
            expected_size=1,
            input_sha256="a" * 64,
            pipeline=TopicJob.Pipeline.INTEREST_V1,
            status=TopicJob.Status.PUBLISHING,
        )
        detail = safe_job_path(job.id, "base/detail.csv", create_parent=True)
        fields = [
            "source_row", "clientId", "cid", "sceneId", "created_at", "text", "ai_text", "turn_index",
            "is_template", "is_invalid_turn", "invalid_reason", "profile_age", "profile_gender",
            "age_status", "gender_status", "city_normalized", "city_status",
        ]
        with detail.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerow({
                "source_row": 1, "clientId": "private-user", "cid": "private-session", "sceneId": "488",
                "created_at": "2026-08-25T12:00:00+08:00",
                "text": "海底新奇梗", "ai_text": "", "turn_index": 1, "is_template": "否",
                "is_invalid_turn": "否", "invalid_reason": "", "profile_age": "6",
                "profile_gender": "男", "age_status": "正常", "gender_status": "正常",
                "city_normalized": "北京", "city_status": "正常",
            })
        manifest = safe_job_path(job.id, "base/manifest.json", create_parent=True)
        manifest.write_text(json.dumps({
            "schemaVersion": "teeni-base-bundle-manifest/1.0.0",
            "contractVersion": "teeni-base-detail/1.2.0",
            "sceneId": "488",
            "outputs": [{
                "role": "primary_detail",
                "file": detail.name,
                "sha256": hashlib.sha256(detail.read_bytes()).hexdigest(),
                "rows": 1,
            }],
        }), encoding="utf-8")
        frozen = freeze_registry(job)
        output = safe_job_path(job.id, "interest", create_parent=True)
        artifacts = analyze(
            detail,
            manifest,
            safe_job_path(job.id, frozen.relative_path),
            output,
            data_date="2026-08-25",
            candidate_min_users=1,
            candidate_min_sessions=1,
            enricher=enricher,
        )
        root = job_directory(job.id).resolve()
        job.runtime_state = {
            "base": {
                "primaryDetailPath": detail.resolve().relative_to(root).as_posix(),
                "manifestPath": manifest.resolve().relative_to(root).as_posix(),
            },
            "interestRegistry": {
                "registryPath": frozen.relative_path,
                "registryVersion": frozen.registry_version,
                "registrySha256": frozen.sha256,
            },
            "interest": {
                "workbookPath": artifacts.workbook_path.resolve().relative_to(root).as_posix(),
                "detailPath": artifacts.detail_path.resolve().relative_to(root).as_posix(),
                "privateDetailPath": artifacts.private_detail_path.resolve().relative_to(root).as_posix(),
                "snapshotPath": artifacts.snapshot_path.resolve().relative_to(root).as_posix(),
                "segmentPath": artifacts.segment_path.resolve().relative_to(root).as_posix(),
                "candidatePath": artifacts.candidate_path.resolve().relative_to(root).as_posix(),
                "manifestPath": artifacts.manifest_path.resolve().relative_to(root).as_posix(),
            },
        }
        job.save(update_fields=["runtime_state", "updated_at"])
        return job, artifacts

    def test_publishing_preserves_model_recommendation(self):
        class Recommend(LocalCandidatePassthroughEnricher):
            def enrich(self, candidates):
                return [{**row, "accepted": True} for row in super().enrich(candidates)]

        job, _ = self._interest_job(enricher=Recommend())
        publish_interest_job(job)
        self.assertTrue(InterestCandidate.objects.get(job=job).model_recommended)

    def test_publish_creates_independent_snapshot_artifacts_and_aggregate_candidates(self):
        legacy_job = TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-25",
            original_name="legacy.csv",
            expected_size=1,
            pipeline=TopicJob.Pipeline.LEGACY_TOPIC,
        )
        legacy = TopicSnapshot.objects.create(
            data_date=legacy_job.data_date,
            job=legacy_job,
            taxonomy_version="7.0.0",
            catalog_revision="old",
            report_schema="teeni-topic-report/4.0.0",
            source_sha256="a" * 64,
            workbook_sha256="b" * 64,
            payload={"legacy": True},
        )
        job, artifacts = self._interest_job()
        snapshot = publish_interest_job(job)
        self.assertEqual(snapshot.schema_version, "teeni-interest-snapshot/1.5.0")
        self.assertEqual(InterestSegmentSnapshot.objects.filter(job=job).count(), 7)
        self.assertEqual(InterestDailyContribution.objects.filter(data_date=job.data_date).count(), 1)
        self.assertTrue(TopicSnapshot.objects.filter(pk=legacy.pk, payload={"legacy": True}).exists())
        self.assertEqual(job.artifacts.filter(verified=True).count(), 2)
        self.assertEqual(
            set(job.artifacts.values_list("kind", flat=True)),
            {TopicArtifact.Kind.INTEREST_WORKBOOK, TopicArtifact.Kind.INTEREST_DETAIL_ZIP},
        )
        self.assertTrue(artifacts.private_detail_path.is_file())
        self.assertNotIn("artifacts", artifacts.private_detail_path.parts)
        candidate = InterestCandidate.objects.get(job=job)
        self.assertEqual(candidate.phrase, "海底新奇梗")
        self.assertFalse(hasattr(candidate, "examples"))

    def test_public_payload_rejects_raw_identifier_keys(self):
        job, artifacts = self._interest_job()
        payload = dict(artifacts.snapshot)
        payload["clientId"] = "private-user"
        with self.assertRaises(InterestSnapshotContractError):
            validate_interest_payload(payload, job)

    def test_current_payload_requires_demographic_contract_and_rejects_profile_fields(self):
        job, artifacts = self._interest_job()
        missing = dict(artifacts.snapshot)
        missing.pop("demographicInterest")
        with self.assertRaises(InterestSnapshotContractError):
            validate_interest_payload(missing, job)

        missing_segments = dict(artifacts.snapshot)
        missing_segments.pop("segmentInterest")
        with self.assertRaises(InterestSnapshotContractError):
            validate_interest_payload(missing_segments, job)

        wrong_groups = json.loads(json.dumps(artifacts.snapshot))
        wrong_groups["demographicInterest"]["groups"].pop()
        with self.assertRaises(InterestSnapshotContractError):
            validate_interest_payload(wrong_groups, job)

        raw_profile = json.loads(json.dumps(artifacts.snapshot))
        raw_profile["demographicInterest"]["groups"][0]["profile_age"] = 6
        with self.assertRaises(InterestSnapshotContractError):
            validate_interest_payload(raw_profile, job)

    def test_legacy_snapshot_remains_readable_from_interest_api(self):
        job, artifacts = self._interest_job()
        payload = json.loads(json.dumps(artifacts.snapshot))
        payload["schemaVersion"] = "teeni-interest-snapshot/1.1.0"
        payload["engineVersion"] = "teeni-interest-engine/1.1.0"
        payload["behaviors"] = [row for row in payload["behaviors"] if row["name"] != "未识别行为"]
        payload["reportSchema"] = "teeni-interest-report/1.1.0"
        payload.pop("demographicInterest")
        payload.pop("segmentInterest")
        validate_interest_payload(payload, job)
        InterestSnapshot.objects.create(
            data_date=job.data_date,
            job=job,
            schema_version=payload["schemaVersion"],
            engine_version=payload["engineVersion"],
            source_sha256=payload["sourceSha256"],
            registry_sha256=payload["registrySha256"],
            workbook_sha256="f" * 64,
            payload=payload,
        )
        self.client.force_login(self.user)

        response = self.client.get("/api/interests/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["schemaVersion"], "teeni-interest-snapshot/1.1.0")
        self.assertNotIn("demographicInterest", response.json()["items"][0])

    def test_previous_age_band_snapshot_contract_remains_readable(self):
        job, artifacts = self._interest_job()
        payload = json.loads(json.dumps(artifacts.snapshot))
        payload["schemaVersion"] = "teeni-interest-snapshot/1.2.0"
        payload["engineVersion"] = "teeni-interest-engine/1.2.0"
        payload["behaviors"] = [row for row in payload["behaviors"] if row["name"] != "未识别行为"]
        payload["reportSchema"] = "teeni-interest-report/1.2.0"
        payload.pop("segmentInterest")
        payload["demographicInterest"] = {
            "overallEligibleUsers": 0,
            "overallIps": [],
            "groups": [
                {
                    "ageBand": age_band,
                    "gender": gender,
                    "groupUsers": 0,
                    "eligibleUsers": 0,
                    "sampleStatus": "样本不足",
                    "ips": [],
                }
                for age_band in ("1-2岁", "3-4岁", "5-6岁", "7-9岁", "10-17岁")
                for gender in ("男", "女")
            ],
        }

        validate_interest_payload(payload, job)

    def test_previous_exact_age_snapshot_contract_remains_readable(self):
        job, artifacts = self._interest_job()
        payload = json.loads(json.dumps(artifacts.snapshot))
        payload["schemaVersion"] = "teeni-interest-snapshot/1.3.0"
        payload["engineVersion"] = "teeni-interest-engine/1.3.0"
        payload["behaviors"] = [row for row in payload["behaviors"] if row["name"] != "未识别行为"]
        payload["reportSchema"] = "teeni-interest-report/1.3.0"
        payload.pop("segmentInterest")

        validate_interest_payload(payload, job)

    def test_shadow_job_is_an_allowed_interest_snapshot_source(self):
        job, _ = self._interest_job()
        job.pipeline = TopicJob.Pipeline.SHADOW
        job.save(update_fields=["pipeline"])

        snapshot = publish_interest_job(job)

        self.assertEqual(snapshot.job_id, job.id)

    def test_backfill_publish_preserves_reviewed_candidates(self):
        job, _ = self._interest_job()
        publish_interest_job(job)
        candidate = InterestCandidate.objects.get(job=job)
        review_candidate(candidate, self.user, {"action": "reject"})

        publish_interest_job(job, require_publishing=False, replace_candidates=False)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, InterestCandidate.Status.REJECTED)
        self.assertEqual(candidate.review_events.count(), 1)

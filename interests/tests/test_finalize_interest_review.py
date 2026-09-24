import csv
import json
import tempfile
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from interest_engine.contracts import sha256_file
from interests.models import InterestCandidate, InterestRegistrySnapshot, InterestSnapshot
from interests.registry import SEED_PATH
from topics.models import TopicJob


class FinalizeInterestReviewTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = override_settings(TEENI_TOPIC_STORAGE_ROOT=self.root / "storage")
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        get_user_model().objects.create_superuser(
            "finalize-admin@example.com",
            password="a-strong-password-42",
        )
        start = date(2026, 8, 24)
        packages = []
        daily = []
        fields = [
            "source_row", "clientId", "cid", "sceneId", "created_at", "text", "ai_text", "turn_index",
            "is_template", "is_invalid_turn", "invalid_reason",
        ]
        for offset in range(7):
            data_date = start + timedelta(days=offset)
            detail = self.root / f"detail-{data_date}.csv"
            with detail.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "source_row": 1,
                    "clientId": "u1",
                    "cid": "s1",
                    "sceneId": "488",
                    "created_at": f"{data_date}T12:00:00+08:00",
                    "text": "奥特曼",
                    "ai_text": "",
                    "turn_index": 1,
                    "is_template": "否",
                    "is_invalid_turn": "否",
                    "invalid_reason": "",
                })
            base_manifest = self.root / f"base-manifest-{data_date}.json"
            base_manifest.write_text(json.dumps({
                "schemaVersion": "teeni-base-bundle-manifest/1.0.0",
                "contractVersion": "teeni-base-detail/1.2.0",
                "sceneId": "488",
                "outputs": [{
                    "role": "primary_detail",
                    "file": detail.name,
                    "sha256": sha256_file(detail),
                    "rows": 1,
                }],
            }), encoding="utf-8")
            packages.append({
                "dataDate": data_date.isoformat(),
                "detailPath": detail.name,
                "manifestPath": base_manifest.name,
                "detailSha256": sha256_file(detail),
                "manifestSha256": sha256_file(base_manifest),
            })
            snapshot = self.root / f"snapshot-{data_date}.json"
            snapshot.write_text(json.dumps({"dataDate": data_date.isoformat()}), encoding="utf-8")
            daily.append({
                "dataDate": data_date.isoformat(),
                "snapshotPath": snapshot.name,
                "snapshotSha256": sha256_file(snapshot),
                "manifestPath": base_manifest.name,
                "manifestSha256": sha256_file(base_manifest),
            })

        self.batch = self.root / "batch.json"
        self.batch.write_text(json.dumps({
            "schemaVersion": "teeni-interest-batch/1.0.0",
            "registryPath": str(SEED_PATH),
            "registrySha256": sha256_file(SEED_PATH),
            "candidateReviewComplete": False,
            "packages": packages,
        }), encoding="utf-8")
        candidate_path = self.root / "candidates.json"
        candidate_path.write_text(json.dumps({
            "schemaVersion": "teeni-interest-batch-candidates/1.0.0",
            "registrySha256": sha256_file(SEED_PATH),
            "degraded": False,
            "candidates": [{
                "phrase": "测试候选",
                "normalizedPhrase": "测试候选",
                "queryCount": 8,
                "users": 5,
                "sessions": 8,
                "sevenDayGrowth": None,
                "accepted": False,
                "suggestedName": "测试候选",
                "suggestedType": "其他",
                "suggestedSubtype": "其他热梗",
                "suggestedParentName": "",
                "suggestedAliases": [],
                "modelConfidence": "低",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        result = self.root / "result.json"
        result.write_text(json.dumps({
            "schemaVersion": "teeni-interest-batch-result/1.0.0",
            "sourceManifestSha256": sha256_file(self.batch),
            "registrySha256": sha256_file(SEED_PATH),
            "candidateReviewComplete": False,
            "candidateDegraded": False,
            "dates": [(start + timedelta(days=offset)).isoformat() for offset in range(7)],
            "daily": daily,
            "candidatePath": candidate_path.name,
            "candidateSha256": sha256_file(candidate_path),
        }), encoding="utf-8")
        call_command("stage_interest_candidates", result=str(result), stdout=StringIO())
        self.job = TopicJob.objects.get(pipeline=TopicJob.Pipeline.INTEREST_V1)

    def test_requires_complete_review_then_freezes_registry_and_manifest(self):
        output = self.root / "batch.reviewed.json"
        with self.assertRaisesRegex(CommandError, "1 pending"):
            call_command(
                "finalize_interest_review",
                job=str(self.job.id),
                manifest=str(self.batch),
                output_manifest=str(output),
            )
        InterestCandidate.objects.filter(job=self.job).update(status=InterestCandidate.Status.REJECTED)
        stdout = StringIO()
        call_command(
            "finalize_interest_review",
            job=str(self.job.id),
            manifest=str(self.batch),
            output_manifest=str(output),
            stdout=stdout,
        )
        result = json.loads(stdout.getvalue())
        reviewed = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result["reviewedCandidates"], 1)
        self.assertTrue(reviewed["candidateReviewComplete"])
        self.assertEqual(reviewed["registryPath"], "teeni-interest-registry.M1.reviewed.json")
        self.assertEqual(reviewed["registrySha256"], sha256_file(self.root / reviewed["registryPath"]))
        self.assertEqual(InterestRegistrySnapshot.objects.filter(job=self.job).count(), 1)
        self.assertEqual(InterestSnapshot.objects.count(), 0)
        self.job.refresh_from_db()
        self.assertTrue(self.job.runtime_state["interestCandidateStaging"]["candidateReviewComplete"])

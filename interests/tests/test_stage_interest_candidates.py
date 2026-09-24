import hashlib
import json
import tempfile
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from interest_engine.contracts import sha256_file
from interests.models import InterestCandidate, InterestSnapshot
from interests.registry import SEED_PATH
from topics.models import TopicArtifact, TopicJob


class StageInterestCandidatesTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = override_settings(TEENI_TOPIC_STORAGE_ROOT=self.root / "storage")
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        get_user_model().objects.create_superuser(
            "candidate-admin@example.com",
            password="a-strong-password-42",
        )
        self.candidate_path = self.root / "candidates.json"
        self.candidate_payload = {
            "schemaVersion": "teeni-interest-batch-candidates/1.0.0",
            "registrySha256": sha256_file(SEED_PATH),
            "degraded": False,
            "candidates": [
                {
                    "phrase": "海底新奇梗",
                    "normalizedPhrase": "海底新奇梗",
                    "queryCount": 12,
                    "users": 7,
                    "sessions": 9,
                    "sevenDayGrowth": 3.5,
                    "accepted": True,
                    "suggestedName": "海底新奇梗",
                    "suggestedType": "网络热梗",
                    "suggestedSubtype": "网络句式",
                    "suggestedParentName": "",
                    "suggestedAliases": ["海底梗"],
                    "modelConfidence": "中",
                    "examples": ["这段私有去标识上下文不能进入数据库或 API"],
                },
                {
                    "phrase": "普通玩法",
                    "normalizedPhrase": "普通玩法",
                    "queryCount": 8,
                    "users": 5,
                    "sessions": 8,
                    "sevenDayGrowth": None,
                    "accepted": False,
                    "suggestedName": "普通玩法",
                    "suggestedType": "其他",
                    "suggestedSubtype": "其他热梗",
                    "suggestedParentName": "",
                    "suggestedAliases": [],
                    "modelConfidence": "低",
                    "examples": [],
                },
            ],
        }
        self._write_candidate_package()

        start = date(2026, 8, 24)
        self.dates = [start + timedelta(days=offset) for offset in range(7)]
        daily = []
        for data_date in self.dates:
            snapshot = self.root / f"snapshot-{data_date}.json"
            manifest = self.root / f"manifest-{data_date}.json"
            snapshot.write_text(json.dumps({"dataDate": data_date.isoformat()}), encoding="utf-8")
            manifest.write_text(json.dumps({"dataDate": data_date.isoformat()}), encoding="utf-8")
            daily.append({
                "dataDate": data_date.isoformat(),
                "snapshotPath": snapshot.name,
                "snapshotSha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                "manifestPath": manifest.name,
                "manifestSha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            })
        self.result_path = self.root / "result.json"
        self.result_payload = {
            "schemaVersion": "teeni-interest-batch-result/1.0.0",
            "registrySha256": sha256_file(SEED_PATH),
            "candidateReviewComplete": False,
            "candidateDegraded": False,
            "dates": [value.isoformat() for value in self.dates],
            "daily": daily,
            "candidatePath": self.candidate_path.name,
            "candidateSha256": hashlib.sha256(self.candidate_path.read_bytes()).hexdigest(),
        }
        self._write_result()

    def _write_candidate_package(self):
        self.candidate_path.write_text(
            json.dumps(self.candidate_payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def _write_result(self):
        self.result_path.write_text(json.dumps(self.result_payload), encoding="utf-8")

    def test_stages_aggregate_candidates_without_publishing_and_is_idempotent(self):
        stdout = StringIO()
        call_command("stage_interest_candidates", result=str(self.result_path), stdout=stdout)
        output = json.loads(stdout.getvalue())
        self.assertTrue(output["created"])
        self.assertEqual(output["candidates"], 2)
        self.assertEqual(output["modelRecommended"], 1)
        self.assertEqual(TopicJob.objects.filter(pipeline=TopicJob.Pipeline.INTEREST_V1).count(), 1)
        self.assertEqual(InterestCandidate.objects.count(), 2)
        self.assertEqual(InterestSnapshot.objects.count(), 0)
        self.assertEqual(TopicArtifact.objects.count(), 0)

        job = TopicJob.objects.get(pipeline=TopicJob.Pipeline.INTEREST_V1)
        self.assertEqual(job.status, TopicJob.Status.INTEREST_VERIFYING)
        self.assertEqual(job.runtime_state["interestCandidateStaging"]["dates"][0], "2026-08-24")
        candidate = InterestCandidate.objects.get(normalized_phrase="海底新奇梗")
        self.assertTrue(candidate.model_recommended)
        self.assertNotIn("examples", candidate.__dict__)
        candidate.status = InterestCandidate.Status.REJECTED
        candidate.save(update_fields=["status"])

        second = StringIO()
        call_command("stage_interest_candidates", result=str(self.result_path), stdout=second)
        self.assertFalse(json.loads(second.getvalue())["created"])
        self.assertEqual(TopicJob.objects.filter(pipeline=TopicJob.Pipeline.INTEREST_V1).count(), 1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, InterestCandidate.Status.REJECTED)

    def test_m2_review_package_accepts_six_days_and_rejects_mixed_candidates(self):
        self.result_payload["dates"] = self.result_payload["dates"][:6]
        self.result_payload["daily"] = self.result_payload["daily"][:6]
        self.result_payload.update({"productVersion": "M2", "sceneId": "904"})
        for row in self.result_payload["daily"]:
            for key in ("snapshot", "manifest"):
                path = self.root / row[key + "Path"]
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload.update({"productVersion": "M2", "sceneId": "904"})
                path.write_text(json.dumps(payload), encoding="utf-8")
                row[key + "Sha256"] = sha256_file(path)
        self._write_result()
        with self.assertRaisesRegex(CommandError, "candidate package product version mismatch"):
            call_command("stage_interest_candidates", result=str(self.result_path), stdout=StringIO())
        self.assertEqual(TopicJob.objects.count(), 0)
        self.candidate_payload.update({"productVersion": "M2", "sceneId": "904"})
        self._write_candidate_package()
        self.result_payload["candidateSha256"] = sha256_file(self.candidate_path)
        self._write_result()
        call_command("stage_interest_candidates", result=str(self.result_path), stdout=StringIO())
        job = TopicJob.objects.get()
        self.assertEqual(job.product_version, "M2")
        self.assertEqual(len(job.runtime_state["interestCandidateStaging"]["dates"]), 6)

    def test_candidate_hash_failure_stages_nothing(self):
        self.candidate_payload["candidates"][0]["users"] = 8
        self._write_candidate_package()
        with self.assertRaisesRegex(CommandError, "candidate package SHA-256 mismatch"):
            call_command("stage_interest_candidates", result=str(self.result_path))
        self.assertEqual(TopicJob.objects.count(), 0)
        self.assertEqual(InterestCandidate.objects.count(), 0)

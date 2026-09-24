import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from interest_engine import ENGINE_VERSION
from topics.models import TopicJob
from topics.runtime import AnalysisRuntime, TopicInputError, _safe_environment
from topics.storage import safe_job_path, upload_path


class RuntimeCredentialBoundaryTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.storage = root / "storage"
        self.skill = root / "skill"
        for name in ("scripts", "references", "vendor"):
            (self.skill / name).mkdir(parents=True, exist_ok=True)
        self.secret = root / "model.key"
        self.secret.write_text("secret-for-test", encoding="utf-8")
        self.secret.chmod(0o600)
        self.settings = override_settings(
            TEENI_TOPIC_STORAGE_ROOT=self.storage,
            TEENI_BASE_SKILL_ROOT=str(self.skill),
            TEENI_TOPICS_API_KEY_FILE=str(self.secret),
            TEENI_TOPICS_BASE_URL="https://model.invalid/v1",
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        self.user = get_user_model().objects.create_user("runtime@example.com", password="a-strong-password-42")
        self.job = TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-20",
            original_name="scene488.csv",
            expected_size=1,
            runtime_state={
                "base": {"primaryDetailPath": "base/detail.csv", "manifestPath": "base/manifest.json"}
            },
        )
        safe_job_path(self.job.id, "base/detail.csv", create_parent=True).write_text("detail", encoding="utf-8")
        safe_job_path(self.job.id, "base/manifest.json", create_parent=True).write_text("{}", encoding="utf-8")

    def test_validate_upload_accepts_tab_delimited_csv_export(self):
        content = (
            "id\tclientId\tcid\ttext\tresponse\ttimestamp\tintention\t"
            "subIntention\tcreated_at\tsceneId\tmodel\n"
            "1\tclient-1\tcid-1\thello\t{}\t1\tintent\tsub\t"
            "2026-08-20 12:00:00\t488\tmodel-1\n"
        ).encode("utf-8")
        job = TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-20",
            original_name="tab-export.csv",
            expected_size=len(content),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
        upload_path(job.id, create=True).write_bytes(content)
        job.refresh_from_db()

        result = AnalysisRuntime().validate_upload(job)

        self.assertEqual(result["rowCount"], 1)
        self.assertEqual(result["inputSha256"], job.expected_sha256)
        self.assertEqual(result["inputPath"], "input/scene488.csv")
        self.assertTrue(safe_job_path(job.id, result["inputPath"]).is_file())

    def test_m2_upload_validates_scene_not_filename(self):
        for scene, accepted in (("904", True), ("488", False)):
            content = (
                "id,clientId,cid,text,response,timestamp,intention,subIntention,created_at,sceneId,model\n"
                f"1,u1,s1,hello,{{}},1,intent,sub,2026-08-20 12:00:00,{scene},test\n"
            ).encode()
            job = TopicJob.objects.create(
                created_by=self.user, data_date="2026-08-20", original_name="scene488.csv",
                product_version="M2", expected_size=len(content), expected_sha256=hashlib.sha256(content).hexdigest(),
            )
            upload_path(job.id, create=True).write_bytes(content)
            job.refresh_from_db()
            if accepted:
                result = AnalysisRuntime().validate_upload(job)
                self.assertEqual(result["inputPath"], "input/scene904.csv")
            else:
                with self.assertRaises(TopicInputError):
                    AnalysisRuntime().validate_upload(job)

    def test_interest_runtime_gives_model_key_only_to_bounded_candidate_analysis(self):
        self.job.pipeline = TopicJob.Pipeline.INTEREST_V1
        self.job.runtime_state["base"] = {
            "primaryDetailPath": "base/detail.csv",
            "manifestPath": "base/manifest.json",
        }
        self.job.save(update_fields=["pipeline", "runtime_state", "updated_at"])
        runtime = AnalysisRuntime()
        frozen = runtime.freeze_interest(self.job)
        self.job.runtime_state["interestRegistry"] = frozen
        self.job.save(update_fields=["runtime_state", "updated_at"])
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs["env"]))
            if "analyze" in command:
                output_dir = Path(command[command.index("--output-dir") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                for name in (
                    "teeni-interest-report.xlsx",
                    "teeni-interest-detail.csv",
                    "teeni-interest-detail.safe.csv",
                    "teeni-interest-candidates.private.json",
                    "teeni-interest-segments.json",
                    "teeni-interest-manifest.json",
                ):
                    (output_dir / name).write_bytes(b"test")
                (output_dir / "teeni-interest-snapshot.json").write_text(json.dumps({
                    "engineVersion": ENGINE_VERSION,
                    "totals": {"inputQueries": 10},
                    "candidates": [],
                    "method": {"candidateDegraded": False, "candidateFailureCode": None},
                }), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "ok": True,
                    "manifestPath": str(output_dir / "teeni-interest-manifest.json"),
                }), stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "ok": True, "detailRows": 10,
            }), stderr="")

        runtime.runner = runner
        result = runtime.run_interest(self.job)
        self.job.runtime_state["interest"] = result
        self.job.save(update_fields=["runtime_state", "updated_at"])
        runtime.verify_interest(self.job)

        self.assertIn("--enable-candidate-model", calls[0][0])
        self.assertEqual(calls[0][1]["TEENI_TOPICS_API_KEY"], "secret-for-test")
        self.assertNotIn("TEENI_TOPICS_API_KEY", calls[1][1])
        self.assertNotIn("/models", " ".join(calls[0][0]))

    def test_retired_classifier_is_not_callable(self):
        runtime = AnalysisRuntime()
        for name in ("run_semantic", "verify_topics", "check_model"):
            self.assertFalse(hasattr(runtime, name))

    def test_base_environment_excludes_model_credentials(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"TEENI_TOPICS_API_KEY": "must-not-leak"}):
            self.assertNotIn("TEENI_TOPICS_API_KEY", _safe_environment())

    def test_interest_runs_without_candidate_key(self):
        with override_settings(TEENI_TOPICS_API_KEY_FILE=""):
            environment, enabled = AnalysisRuntime()._interest_environment()
        self.assertFalse(enabled)
        self.assertNotIn("TEENI_TOPICS_API_KEY", environment)

import copy
import hashlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from dashboard.management.commands.import_opening_cohorts import canonical_sha256, _record, _write_json
from dashboard.models import DailySnapshot, ManagementInsight, ManagementInsightRevision, ProductResearchSnapshot
from interests.models import InterestDailyContribution
from publisher.opening_cohorts import GROUP_KEYS, RULES_SHA256, validate_opening_cohorts
from publisher.session_structure import COUNTS, RATIOS, SCHEMA as STRUCTURE_SCHEMA


COMMAND = "dashboard.management.commands.import_opening_cohorts"


class OpeningCohortsImportTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.summary = self.directory / "summary.json"
        self.backups = self.directory / "backups"
        structure = {"schemaVersion": STRUCTURE_SCHEMA, **dict.fromkeys(COUNTS, 0), **dict.fromkeys(RATIOS)}
        self.payload = {
            "dataDate": "2026-09-13", "productVersion": "M1",
            "sourceSha256": "a" * 64, "workbookSha256": "b" * 64,
            "baseManifestSha256": "c" * 64, "baseDetailSha256": "d" * 64,
            "metrics": {"sessionStructure": structure,
                        "opening": {"rate": 0.25}, "interest": {"preserve": True}},
            "preservedProvenance": {"sha256": "f" * 64},
        }
        self.base = DailySnapshot.objects.create(
            data_date="2026-09-13", product_version="M1", scene_id="488", core_version="2.7.0",
            rules_version="16.0.0", contract_version="teeni-base-detail/1.2.0",
            source_sha256="a" * 64, workbook_sha256="b" * 64, payload=self.payload,
        )
        self.addon = {
            "schemaVersion": "teeni-opening-cohorts/1.0.0", "productVersion": "M1", "dataDate": "2026-09-13",
            "structureVersion": STRUCTURE_SCHEMA, "rulesSha256": RULES_SHA256, "sceneId": "488",
            "source": {"sourceSha256": "a" * 64, "workbookSha256": "b" * 64,
                       "manifestSha256": "c" * 64, "detailSha256": "d" * 64, "calculatorSha256": "e" * 64},
            "totals": structure,
            "groups": [{"key": key, **dict.fromkeys(COUNTS, 0), **dict.fromkeys(RATIOS),
                        "replySessions": 0, "replyRate": None, "sessionShare": None} for key in GROUP_KEYS],
            "diagnostics": {"emptyFirstSessions": 0, "systemHintFirstSessions": 0},
            "crossDay": {"status": "unavailable", "previousDate": "2026-09-12", "overlapSessions": None,
                         "previousDetailSha256": None, "previousManifestSha256": None},
        }
        self.expected_sha = canonical_sha256(self.payload)
        self.validation = patch(f"{COMMAND}.validate_opening_cohorts", wraps=validate_opening_cohorts).start()
        self.addCleanup(patch.stopall)

    def invoke(self, apply=False, **overrides):
        self.summary.write_text(json.dumps(self.addon), encoding="utf-8")
        options = {
            "summary": str(self.summary), "sha256": hashlib.sha256(self.summary.read_bytes()).hexdigest(),
            "product_version": "M1", "data_date": "2026-09-13",
            "expected_payload_sha256": self.expected_sha, "backup_dir": str(self.backups), "apply": apply,
        }
        options.update(overrides)
        output = io.StringIO()
        call_command("import_opening_cohorts", stdout=output, **options)
        return json.loads(output.getvalue())

    def test_dry_run_validates_without_writing_files_or_database(self):
        before = _record(self.base)
        result = self.invoke()
        self.base.refresh_from_db()
        self.assertEqual(_record(self.base), before)
        self.assertEqual(result["status"], "validated")
        self.assertFalse(self.backups.exists())
        self.validation.assert_called_once_with(self.addon, self.payload["metrics"]["sessionStructure"])

    def test_apply_preserves_other_payload_fields_metadata_and_related_rows(self):
        before = _record(self.base)
        ProductResearchSnapshot.objects.create(product_version="M1", data_date="2026-09-13", source_sha256="e" * 64,
                                               payload={"aiEvaluation": {"sealed": True}, "sources": ["preserved"]})
        insight = ManagementInsight.objects.create(product_version="M1", start_date="2026-09-13", end_date="2026-09-13")
        ManagementInsightRevision.objects.create(insight=insight, revision=1, title="preserved", summary="preserved")
        InterestDailyContribution.objects.create(product_version="M1", data_date="2026-09-13", source_sha256="a" * 64,
                                                 registry_sha256="e" * 64, identity_sha256="e" * 64,
                                                 detail_sha256="d" * 64, payload=b"sealed contribution")
        protected = [ProductResearchSnapshot, ManagementInsight, ManagementInsightRevision, InterestDailyContribution]
        prior_rows = [list(model.objects.values()) for model in protected]
        result = self.invoke(apply=True)
        self.base.refresh_from_db()
        expected = copy.deepcopy(before)
        expected["payload"]["metrics"]["openingCohorts"] = self.addon
        self.assertEqual(_record(self.base), expected)
        self.assertEqual([list(model.objects.values()) for model in protected], prior_rows)
        self.assertEqual(result["status"], "published")
        backup = json.loads(next(self.backups.glob("*.backup.json")).read_text(encoding="utf-8"))
        self.assertEqual(backup["snapshot"], before)
        self.assertEqual(result["afterPayloadSha256"], canonical_sha256(expected["payload"]))

    def test_source_hashes_must_match_published_payload(self):
        for key in ("sourceSha256", "workbookSha256", "manifestSha256", "detailSha256"):
            with self.subTest(key=key):
                original = self.addon["source"][key]
                self.addon["source"][key] = "f" * 64
                with self.assertRaisesRegex(CommandError, "source mismatch"):
                    self.invoke(apply=True)
                self.addon["source"][key] = original
        self.assertFalse(self.backups.exists())

    def test_source_columns_and_explicit_target_must_match(self):
        with self.assertRaisesRegex(CommandError, "explicit target"):
            self.invoke(product_version="M2")
        with self.assertRaisesRegex(CommandError, "explicit target"):
            self.invoke(data_date="2026-09-12")
        DailySnapshot.objects.filter(pk=self.base.pk).update(source_sha256="f" * 64)
        with self.assertRaisesRegex(CommandError, "source columns"):
            self.invoke(apply=True)

    def test_verified_previous_day_must_still_match_published_source(self):
        self.addon["crossDay"].update(status="verified", overlapSessions=0,
                                    previousDetailSha256="d" * 64, previousManifestSha256="c" * 64)
        with self.assertRaisesRegex(CommandError, "previous-day source"):
            self.invoke(apply=True)
        prior = DailySnapshot.objects.create(
            data_date="2026-09-12", product_version="M1", scene_id="488", core_version="2.7.0",
            rules_version="16.0.0", contract_version="teeni-base-detail/1.2.0",
            source_sha256="a" * 64, workbook_sha256="b" * 64, payload={"baseDetailSha256": "d" * 64, "baseManifestSha256": "c" * 64},
        )
        self.assertEqual(self.invoke()["status"], "validated")
        prior.payload["baseManifestSha256"] = "f" * 64
        prior.save(update_fields=["payload"])
        with self.assertRaisesRegex(CommandError, "previous-day source"):
            self.invoke(apply=True)

    def test_concurrent_unrelated_payload_change_is_rejected(self):
        changed = copy.deepcopy(self.payload)
        changed["metrics"]["opening"]["rate"] = 0.5
        DailySnapshot.objects.filter(pk=self.base.pk).update(payload=changed)
        with self.assertRaisesRegex(CommandError, "changed since preflight"):
            self.invoke(apply=True)
        self.assertFalse(self.backups.exists())

    def test_exact_replay_recovers_without_modifying_original_backup(self):
        self.invoke(apply=True)
        backup_path = next(self.backups.glob("*.backup.json"))
        original_backup = backup_path.read_bytes()
        self.base.refresh_from_db()
        published = _record(self.base)
        self.assertEqual(self.invoke(apply=True)["status"], "recovered")
        self.base.refresh_from_db()
        self.assertEqual(_record(self.base), published)
        self.assertEqual(backup_path.read_bytes(), original_backup)

    def test_recovery_rejects_unrelated_metadata_changes(self):
        self.invoke(apply=True)
        DailySnapshot.objects.filter(pk=self.base.pk).update(rules_version="17.0.0")
        with self.assertRaisesRegex(CommandError, "metadata changed"):
            self.invoke(apply=True)

    def test_lost_post_commit_receipt_is_recovered_from_database(self):
        def write(path, value):
            if value.get("status") == "published":
                raise OSError("simulated lost final receipt")
            _write_json(path, value)
        with patch(f"{COMMAND}._write_json", side_effect=write):
            with self.assertRaisesRegex(CommandError, "lost final receipt"):
                self.invoke(apply=True)
        self.base.refresh_from_db()
        self.assertEqual(self.base.payload["metrics"]["openingCohorts"], self.addon)
        receipt = json.loads(next(self.backups.glob("*.receipt.json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "pending")
        self.assertEqual(self.invoke(apply=True)["status"], "recovered")

    def test_pending_receipt_failure_leaves_base_unchanged(self):
        def write(path, value):
            if value.get("status") == "pending":
                raise OSError("simulated journal failure")
            _write_json(path, value)
        with patch(f"{COMMAND}._write_json", side_effect=write):
            with self.assertRaisesRegex(CommandError, "journal failure"):
                self.invoke(apply=True)
        self.base.refresh_from_db()
        self.assertEqual(self.base.payload, self.payload)
        self.assertEqual(self.invoke(apply=True)["status"], "published")

    def test_invalid_summary_structure_and_private_content_fail_before_write(self):
        with self.assertRaisesRegex(CommandError, "summary hash"):
            self.invoke(apply=True, sha256="f" * 64)
        self.validation.side_effect = ValueError("structure counts disagree")
        with self.assertRaisesRegex(CommandError, "structure counts"):
            self.invoke(apply=True)
        self.validation.side_effect = None
        self.addon["query"] = "private text"
        with self.assertRaisesRegex(CommandError, "forbidden field"):
            self.invoke(apply=True)
        self.assertFalse(self.backups.exists())

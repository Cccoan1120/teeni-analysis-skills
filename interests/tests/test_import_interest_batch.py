import csv
import hashlib
import json
import tempfile
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from interests.models import InterestDailyContribution, InterestEntity, InterestSegmentSnapshot, InterestSnapshot
from topics.models import TopicJob


class ImportInterestBatchTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = override_settings(TEENI_TOPIC_STORAGE_ROOT=self.root / "中文存储")
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        get_user_model().objects.create_superuser(
            "batch-admin@example.com",
            password="a-strong-password-42",
        )
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps({
            "schemaVersion": "teeni-interest-registry/1.1.0",
            "registryVersion": "batch-publish-test",
            "entities": [{
                "id": "IE1", "canonicalName": "奥特曼", "entityType": "作品", "entitySubtype": "特摄",
                "broadTopic": "影视动漫与角色",
                "matchRules": [{"value": "奥特曼", "mode": "substring", "policy": "auto"}],
            }],
        }, ensure_ascii=False), encoding="utf-8")
        start = date(2026, 8, 21)
        packages = []
        fields = [
            "source_row", "clientId", "cid", "sceneId", "created_at", "text", "ai_text", "turn_index",
            "is_template", "is_invalid_turn", "invalid_reason",
            "profile_age", "age_status", "profile_gender", "gender_status",
            "city_normalized", "city_status",
        ]
        for offset in range(7):
            data_date = start + timedelta(days=offset)
            detail = self.root / f"detail-{data_date}.csv"
            with detail.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "source_row": 1, "clientId": "u1", "cid": "s1", "sceneId": "488",
                    "created_at": f"{data_date}T12:00:00+08:00", "text": "奥特曼", "ai_text": "",
                    "turn_index": 1, "is_template": "否", "is_invalid_turn": "否", "invalid_reason": "",
                    "profile_age": "6", "age_status": "正常", "profile_gender": "男", "gender_status": "正常",
                    "city_normalized": "北京", "city_status": "正常",
                })
            manifest = self.root / f"manifest-{data_date}.json"
            manifest.write_text(json.dumps({
                "schemaVersion": "teeni-base-bundle-manifest/1.0.0",
                "contractVersion": "teeni-base-detail/1.2.0",
                "sceneId": "488",
                "outputs": [{
                    "role": "primary_detail", "file": detail.name,
                    "sha256": hashlib.sha256(detail.read_bytes()).hexdigest(), "rows": 1,
                }],
            }), encoding="utf-8")
            packages.append({
                "dataDate": data_date.isoformat(), "detailPath": detail.name, "manifestPath": manifest.name,
                "detailSha256": hashlib.sha256(detail.read_bytes()).hexdigest(),
                "manifestSha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            })
        self.batch = self.root / "batch.json"
        self.payload = {
            "schemaVersion": "teeni-interest-batch/1.0.0",
            "registryPath": self.registry.name,
            "registrySha256": hashlib.sha256(self.registry.read_bytes()).hexdigest(),
            "candidateReviewComplete": True,
            "packages": packages,
        }
        self.write_batch()

    def write_batch(self):
        self.batch.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")

    def test_m2_same_date_publication_preserves_m1_snapshots_and_contributions(self):
        self.payload["packages"] = self.payload["packages"][:1]
        self.write_batch()
        call_command("import_interest_batch", manifest=str(self.batch), publish=True, stdout=StringIO(), stderr=StringIO())
        m1 = InterestSnapshot.objects.get(product_version="M1")
        original_payload = m1.payload
        original_job = m1.job_id
        package = self.payload["packages"][0]
        detail = self.root / package["detailPath"]
        with detail.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        for row in rows:
            row["sceneId"] = "904"
        with detail.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        digest = hashlib.sha256(detail.read_bytes()).hexdigest()
        manifest = self.root / package["manifestPath"]
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["sceneId"] = "904"
        payload["outputs"][0]["sha256"] = digest
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        package["detailSha256"] = digest
        package["manifestSha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.payload.update({"productVersion": "M2", "sceneId": "904"})
        self.write_batch()
        call_command("import_interest_batch", manifest=str(self.batch), publish=True, stdout=StringIO(), stderr=StringIO())
        m1.refresh_from_db()
        self.assertEqual(m1.payload, original_payload)
        self.assertEqual(m1.job_id, original_job)
        self.assertEqual(InterestSnapshot.objects.count(), 2)
        self.assertEqual(InterestDailyContribution.objects.count(), 2)
        self.assertEqual(InterestSegmentSnapshot.objects.count(), 14)
        m2 = InterestSnapshot.objects.get(product_version="M2")
        self.assertEqual(m2.job.product_version, "M2")
        self.assertEqual(m2.payload["sceneId"], "904")
        self.assertTrue(all("M2" in item.download_name for item in m2.job.artifacts.all()))
        self.client.force_login(get_user_model().objects.get(is_superuser=True))
        for version in ("M1", "M2"):
            response = self.client.get("/api/interests/", {"productVersion": version})
            self.assertEqual(len(response.json()["items"]), 1)
            self.assertEqual(response.json()["items"][0]["productVersion"], version)

    def test_publish_is_atomic_and_same_dates_are_replaced_idempotently(self):
        stdout = StringIO()
        call_command("import_interest_batch", manifest=str(self.batch), publish=True, stdout=stdout)
        stdout.getvalue().encode("ascii")
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["published"])
        self.assertFalse(result["candidateDegraded"])
        self.assertEqual(InterestSnapshot.objects.count(), 7)
        self.assertEqual(InterestSegmentSnapshot.objects.count(), 49)
        self.assertEqual(TopicJob.objects.count(), 7)

        self.assertEqual(
            set(InterestSegmentSnapshot.objects.values_list("dimension_set", flat=True)),
            {"age", "gender", "region", "age_gender", "age_region", "gender_region", "age_gender_region"},
        )
        self.assertEqual(
            set(InterestSnapshot.objects.values_list("registry_sha256", flat=True)),
            {self.payload["registrySha256"]},
        )
        self.assertEqual(
            list(InterestEntity.objects.values_list("registry_id", "canonical_name")),
            [("IE1", "奥特曼")],
        )

        call_command("import_interest_batch", manifest=str(self.batch), publish=True, stdout=StringIO())
        self.assertEqual(InterestSnapshot.objects.count(), 7)
        self.assertEqual(InterestSegmentSnapshot.objects.count(), 49)
        self.assertEqual(TopicJob.objects.count(), 7)

    def test_publish_ready_result_does_not_analyze_or_create_duplicate_jobs(self):
        self.payload["packages"] = self.payload["packages"][:1]
        self.write_batch()
        output = StringIO()
        call_command("import_interest_batch", manifest=str(self.batch), stdout=output)
        ready = json.loads(output.getvalue())["resultManifest"]
        with patch("interests.management.commands.import_interest_batch.analyze_batch", side_effect=AssertionError("reanalysis")):
            for _ in range(2):
                call_command("import_interest_batch", manifest=str(self.batch), result_manifest=ready, publish=True, stdout=StringIO())
        self.assertEqual(InterestSnapshot.objects.count(), 1)
        self.assertEqual(TopicJob.objects.count(), 1)
        self.assertEqual(InterestDailyContribution.objects.count(), 1)
        self.client.force_login(get_user_model().objects.get(is_superuser=True))
        response = self.client.get("/api/interests/preferences/", {"period": "all"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["totals"]["activeInterestUsers"], 1)
        InterestDailyContribution.objects.all().delete()
        with patch("interests.management.commands.import_interest_batch.analyze_batch", side_effect=AssertionError("reanalysis")):
            call_command("backfill_interest_preferences", manifest=[str(self.batch)], apply=False, stdout=StringIO())
            self.assertEqual(InterestDailyContribution.objects.count(), 0)
            call_command("backfill_interest_preferences", manifest=[str(self.batch)], apply=True, stdout=StringIO())
        self.assertEqual(InterestDailyContribution.objects.count(), 1)
        from topics.storage import safe_job_path
        workbook = TopicJob.objects.get().artifacts.get(kind="interest_workbook")
        safe_job_path(workbook.job_id, workbook.relative_path).unlink()
        with patch("interests.management.commands.import_interest_batch.analyze_batch", side_effect=AssertionError("reanalysis")):
            call_command("import_interest_batch", manifest=str(self.batch), result_manifest=ready,
                         backup_dir=str(self.root / "backup"), publish=True, stdout=StringIO())
        self.assertEqual(InterestSnapshot.objects.count(), 1)
        self.assertEqual(TopicJob.objects.count(), 2)
        backup = json.loads((self.root / "backup" / "scoped-backup-receipt.json").read_text())
        self.assertEqual(backup["dates"], ["2026-08-21"])
        self.assertEqual(len(backup["files"]), 5)

    def test_review_gate_or_hash_failure_writes_no_snapshots(self):
        self.payload["candidateReviewComplete"] = False
        self.write_batch()
        with self.assertRaisesRegex(CommandError, "candidateReviewComplete"):
            call_command("import_interest_batch", manifest=str(self.batch), publish=True)
        self.assertEqual(InterestSnapshot.objects.count(), 0)
        self.assertEqual(InterestSegmentSnapshot.objects.count(), 0)

        self.payload["candidateReviewComplete"] = True
        self.payload["packages"][3]["manifestSha256"] = "0" * 64
        self.write_batch()
        with self.assertRaisesRegex(CommandError, "manifest SHA-256"):
            call_command("import_interest_batch", manifest=str(self.batch), publish=True)
        self.assertEqual(InterestSnapshot.objects.count(), 0)
        self.assertEqual(InterestSegmentSnapshot.objects.count(), 0)

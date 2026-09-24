import json
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from interests.models import InterestSegmentSnapshot, InterestSnapshot
from topics.models import TopicJob


class RepairAggregateTests(TestCase):
    def test_repair_only_modifies_selected_product_version(self):
        user = get_user_model().objects.create_user("version-repair@example.com")
        records, segments = {}, {}
        for version, scene in (("M1", "488"), ("M2", "904")):
            job = TopicJob.objects.create(created_by=user, data_date="2026-09-01", product_version=version, original_name="anonymous.csv", expected_size=1)
            payload = {"dataDate": job.data_date, "productVersion": version, "sceneId": scene,
                       "method": {"baseCoreVersion": "core", "baseRulesVersion": "rules"},
                       "demographicInterest": {"groups": [{"eligibleUsers": 1, "sampleStatus": "可描述"}]}}
            records[version] = InterestSnapshot.objects.create(
                job=job, data_date=job.data_date, product_version=version, schema_version="schema", engine_version="engine",
                source_sha256="a" * 64, registry_sha256="b" * 64, workbook_sha256="c" * 64, payload=payload,
            )
            segments[version] = InterestSegmentSnapshot.objects.create(
                job=job, data_date=job.data_date, product_version=version, dimension_set="gender", schema_version="schema",
                source_sha256="a" * 64, registry_sha256="b" * 64, province_map_version="map", province_map_sha256="c" * 64,
                payload={"productVersion": version, "sceneId": scene, "groups": [{"eligibleUsers": 1, "sampleStatus": "可描述"}]},
            )
        original = records["M1"].payload
        with tempfile.TemporaryDirectory() as root:
            output = StringIO()
            call_command("repair_interest_aggregates", output_dir=root, product_version="M2", apply=True, stdout=output)
            result = json.loads(output.getvalue())
            plan = json.loads(Path(result["backupAndPlan"]).read_text(encoding="utf-8"))
            self.assertEqual(plan["productVersion"], "M2")
            self.assertEqual(set(plan["sourceSnapshotGuards"]), {str(records["M2"].pk)})
            self.assertEqual(result["changedRows"], 2)
        for record in [*records.values(), *segments.values()]:
            record.refresh_from_db()
        self.assertEqual(records["M1"].payload, original)
        self.assertEqual(segments["M1"].payload["groups"][0]["sampleStatus"], "可描述")
        self.assertEqual(records["M2"].payload["demographicInterest"]["groups"][0]["sampleStatus"], "样本不足")
        self.assertEqual(segments["M2"].payload["groups"][0]["sampleStatus"], "样本不足")

    def test_dry_run_keeps_database_and_apply_has_exact_backup(self):
        user = get_user_model().objects.create_user("repair@example.com")
        for day in range(1, 9):
            job = TopicJob.objects.create(created_by=user, data_date=f"2026-09-{day:02}", original_name="anonymous.csv", expected_size=1)
            InterestSnapshot.objects.create(
                job=job, data_date=job.data_date, schema_version="schema", engine_version="engine",
                source_sha256="a" * 64, registry_sha256="b" * 64, workbook_sha256="c" * 64,
                payload={"dataDate": job.data_date, "schemaVersion": "schema", "engineVersion": "engine",
                         "detailSchema": "detail", "registrySha256": "b" * 64,
                         "method": {"baseContract": "base", "baseCoreVersion": "core", "baseRulesVersion": "rules"},
                         "entities": [{"id": "one", "activeInterestUsers": 7, "sevenDayChange": 0}] if day in (1, 8) else [],
                         "demographicInterest": {"groups": [{"groupUsers": 60, "eligibleUsers": 1, "sampleStatus": "可描述"}]}},
            )
        record = InterestSnapshot.objects.get(data_date="2026-09-08")
        original = record.payload
        with tempfile.TemporaryDirectory() as root:
            output = StringIO()
            call_command("repair_interest_aggregates", output_dir=root, date="2026-09-08", stdout=output)
            record.refresh_from_db()
            self.assertEqual(record.payload, original)
            plan_path = Path(json.loads(output.getvalue())["backupAndPlan"])
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["changes"][0]["before"], original)
            self.assertEqual(plan["changes"][0]["after"]["entities"][0]["sevenDayChange"], 6)
            call_command("repair_interest_aggregates", output_dir=root, date="2026-09-08", apply=True, stdout=StringIO())
            record.refresh_from_db()
            self.assertEqual(record.payload["entities"][0]["sevenDayChange"], 6)
            self.assertEqual(record.payload["demographicInterest"]["groups"][0]["sampleStatus"], "样本不足")
            self.assertTrue(record.payload["aggregateCorrection"]["originalArtifactUnchanged"])
            self.assertEqual(record.payload["aggregateCorrection"]["originalWorkbookSha256"], "c" * 64)
            second = StringIO()
            call_command("repair_interest_aggregates", output_dir=root, date="2026-09-08", apply=True, stdout=second)
            self.assertEqual(json.loads(second.getvalue())["changedRows"], 0)

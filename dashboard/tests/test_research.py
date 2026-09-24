import csv
import json
import tempfile
import copy
import io
from unittest.mock import patch
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase, SimpleTestCase

from dashboard.models import DailySnapshot, ProductResearchSnapshot
from dashboard.research import longitudinal, ratio, review, confirm_coverage, validate_report, _write_json, recompute_behavior_preserving_evaluation, _evaluation_hash
from interest_engine.contracts import sha256_file


class LongitudinalTests(SimpleTestCase):
    def test_recompute_preserves_sealed_evaluation_and_original_provenance(self):
        day = '2026-09-13'
        old_sources = [{'dataDate': day, 'detailSha256': 'a' * 64, 'manifestSha256': 'b' * 64, 'coverageComplete': False}]
        evaluation = {'status': 'awaiting_review', 'populationSessions': 12, 'selectedSessions': 3,
                      'reviewedSessions': 0, 'decidableSessions': 0, 'coverage': ratio(0, 3), 'completion': ratio(0, 0), 'rows': []}
        report = {'schemaVersion': 'teeni-product-research/1.0.0', 'productVersion': 'M1', 'dataDate': day,
                  'sources': old_sources, 'behavior': longitudinal({day: {'old-account'}}, set()),
                  'systemIntentDemand': {}, 'instrumentation': {}, 'evaluation': evaluation,
                  'aiEvaluation': {**copy.deepcopy(evaluation), 'labelSource': 'ai', 'humanReviewed': False}}
        new_sources = [{**old_sources[0], 'detailSha256': 'c' * 64, 'coverageComplete': True}]
        with tempfile.TemporaryDirectory() as root:
            legacy = Path(root) / 'legacy.json'
            _write_json(legacy, report)
            before = legacy.read_bytes()
            with patch('dashboard.research._read_inputs', return_value=('M1', day, new_sources, {day: set()}, {day}, {}, {'eligibleUsers': 0})):
                result = recompute_behavior_preserving_evaluation('input.json', legacy)
            self.assertEqual(legacy.read_bytes(), before)
            self.assertEqual(result['evaluation'], evaluation)
            self.assertEqual(result['aiEvaluation'], report['aiEvaluation'])
            self.assertEqual(result['evaluationProvenance']['sources'], old_sources)
            self.assertEqual(result['evaluationProvenance']['sourceReportSha256'], sha256_file(legacy))
            self.assertFalse(result['sources'][0]['coverageComplete'])
            self.assertEqual(result['behavior']['daily'][0]['activeUsers'], 0)
            result['evaluation']['status'] = 'changed'
            with self.assertRaisesRegex(ValueError, 'content hash'):
                validate_report(result)

    def test_missing_day_is_unknown_not_zero_and_ai_failures_can_remain_active(self):
        days = {"2026-09-01": {"a", "b"}, "2026-09-02": {"a"}, "2026-09-08": {"a"}}
        result = longitudinal(days, set(days))
        self.assertEqual(result["cohorts"][0]["d1"]["rate"], 0.5)
        self.assertIsNone(result["cohorts"][0]["d7"]["rate"])
        self.assertEqual(result["cohorts"][0]["d7"]["observedRate"], 0.5)
        self.assertEqual(result["daily"][1]["returningUsers"], 1)

    def test_mature_d7_and_zero_denominator(self):
        days = {f"2026-09-{day:02}": {"a"} if day in (1, 8) else set() for day in range(1, 9)}
        result = longitudinal(days, set(days))
        self.assertEqual(result["cohorts"][0]["d7"]["rate"], 1)
        self.assertIsNone(result["cohorts"][1]["d1"]["rate"])
        self.assertIsNone(ratio(0, 0)["rate"])

    def test_review_requires_sealed_rows_and_never_exports_conversation(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            report = {"evaluation": {"selectedSessions": 1}}
            summary = directory / "research-summary.json"
            _write_json(summary, report)
            sample = {"sample_id": "sample", "data_date": "2026-09-07", "sampling_group": "M1/2026-09-07", "selection_probability": "0.5", "conversation": "private synthetic dialogue"}
            _write_json(directory / "review-selection.private.json", {"summarySha256": sha256_file(summary), "samples": {"sample": {**sample, "account": "private-account"}}})
            annotations = directory / "annotations.csv"
            row = {**sample, "demand_type": "知识问答", "outcome": "完整满足", "reviewer": "reviewer", "reviewed_at": "2026-09-08", "notes": "private note"}
            def write():
                with annotations.open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(row)); writer.writeheader(); writer.writerow(row)
            write()
            payload = review(directory, annotations, directory / "reviewed.json")
            self.assertEqual(payload["evaluation"]["completion"]["rate"], 1)
            self.assertNotIn("private", json.dumps(payload))
            row["selection_probability"] = "1"
            write()
            with self.assertRaisesRegex(ValueError, "selection probability"):
                review(directory, annotations, directory / "invalid.json")

    def test_ai_labels_cannot_enter_human_summary(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            summary = directory / 'research-summary.json'
            report = {'evaluation': {'selectedSessions': 1, 'reviewedSessions': 0, 'completion': ratio(0, 0)}}
            _write_json(summary, report)
            sample = {'sample_id': 'sample', 'data_date': '2026-09-07', 'sampling_group': 'M1/2026-09-07', 'selection_probability': '1', 'conversation': 'synthetic'}
            _write_json(directory / 'review-selection.private.json', {'summarySha256': sha256_file(summary), 'samples': {'sample': {**sample, 'account': 'private-account'}}})
            row = {**sample, 'demand_type': '知识问答', 'outcome': '完整满足', 'reviewer': 'Codex', 'reviewed_at': '2026-09-08', 'label_source': 'ai'}
            annotations = directory / 'annotations.csv'
            with annotations.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(row)); writer.writeheader(); writer.writerow(row)
            with self.assertRaisesRegex(ValueError, 'provenance'):
                review(directory, annotations, directory / 'wrong.json')
            result = review(directory, annotations, directory / 'ai.json', 'ai')
            self.assertEqual(result['evaluation'], report['evaluation'])
            self.assertEqual(result['aiEvaluation']['completion']['rate'], 1)
            self.assertFalse(result['aiEvaluation']['humanReviewed'])

    def test_confirmation_preserves_counts_and_future_windows(self):
        days = {f'2026-09-{day:02}': {'a'} for day in range(1, 8)}
        sources = [{'dataDate': day, 'detailSha256': 'a' * 64, 'manifestSha256': 'b' * 64, 'coverageComplete': False} for day in days]
        report = {'schemaVersion': 'teeni-product-research/1.0.0', 'productVersion': 'M1', 'dataDate': '2026-09-07', 'sources': sources,
                  'behavior': longitudinal(days, set()), 'systemIntentDemand': {}, 'instrumentation': {},
                  'evaluation': {'populationSessions': 1, 'selectedSessions': 1, 'reviewedSessions': 0, 'decidableSessions': 0, 'coverage': ratio(0, 1), 'completion': ratio(0, 0), 'rows': []}}
        confirmation = {'basis': 'user_confirmation', 'productVersion': 'M1', 'confirmedAt': '2026-09-08',
                        'sources': [{key: source[key] for key in ('dataDate', 'detailSha256', 'manifestSha256')} for source in sources]}
        result = confirm_coverage(report, confirmation, 'c' * 64)
        self.assertEqual(result['behavior']['daily'], report['behavior']['daily'])
        self.assertEqual(result['evaluation'], report['evaluation'])
        self.assertEqual(result['behavior']['cohorts'][0]['d1']['rate'], 1)
        self.assertIsNone(result['behavior']['cohorts'][0]['d7']['rate'])
        self.assertIsNone(result['behavior']['cohorts'][-1]['d1']['rate'])
        self.assertFalse(report['sources'][0]['coverageComplete'])
        confirmation['sources'][0]['detailSha256'] = 'd' * 64
        with self.assertRaisesRegex(ValueError, 'exact source'):
            confirm_coverage(report, confirmation, 'c' * 64)
        result['behavior']['cohorts'][0]['d7'].update(status='complete', rate=0)
        with self.assertRaisesRegex(ValueError, 'maturity'):
            validate_report(result)


class ResearchApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="research-member", password="local-only-test")

    def test_login_required_and_invalid_product(self):
        self.assertEqual(self.client.get("/api/research/").status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/research/?productVersion=other").status_code, 400)
        self.assertEqual(self.client.get("/api/research/?date=broken").status_code, 400)

    def test_four_statuses_do_not_infer_interest_success_from_base(self):
        DailySnapshot.objects.create(product_version="M1", scene_id="488", data_date="2026-09-07", core_version="2.6.0", rules_version="14.0.0", contract_version="teeni-base-detail/1.2.0", source_sha256="a" * 64, workbook_sha256="b" * 64, payload={"baseDetailSha256": "c" * 64})
        self.client.force_login(self.user)
        result = self.client.get("/api/research/?productVersion=M1&date=2026-09-07").json()
        self.assertEqual(result["pipeline"][0]["base"]["status"], "available")
        self.assertEqual(result["pipeline"][0]["interest"]["status"], "missing")
        self.assertEqual(result["pipeline"][1]["base"]["status"], "missing")
        self.assertIsNone(result["research"])

    def test_historical_evaluation_does_not_make_current_behavior_stale(self):
        day = '2026-09-13'
        base = DailySnapshot.objects.create(product_version='M1', scene_id='488', data_date=day, core_version='2.7.0', rules_version='16.0.0', contract_version='teeni-base-detail/1.2.0', source_sha256='a' * 64, workbook_sha256='b' * 64, payload={'baseDetailSha256': 'c' * 64})
        ProductResearchSnapshot.objects.create(product_version='M1', data_date=day, source_sha256='d' * 64,
            payload={'sources': [{'dataDate': day, 'detailSha256': 'c' * 64}], 'evaluationProvenance': {'status': 'historical_definition', 'sources': [{'dataDate': day, 'detailSha256': 'a' * 64}]}})
        self.client.force_login(self.user)
        result = self.client.get(f'/api/research/?productVersion=M1&date={day}').json()
        self.assertFalse(result['researchStale'])
        self.assertEqual(result['researchEvaluationStatus'], 'historical_definition')
        base.payload['baseDetailSha256'] = 'e' * 64
        base.save(update_fields=['payload'])
        self.assertTrue(self.client.get(f'/api/research/?productVersion=M1&date={day}').json()['researchStale'])

    def test_import_requires_exact_original_evaluation_and_allows_idempotent_retry(self):
        day = '2026-09-13'
        evaluation = {'status': 'awaiting_review', 'populationSessions': 1, 'selectedSessions': 1,
                      'reviewedSessions': 0, 'decidableSessions': 0, 'coverage': ratio(0, 1), 'completion': ratio(0, 0), 'rows': []}
        sources = [{'dataDate': day, 'detailSha256': 'a' * 64, 'manifestSha256': 'b' * 64, 'coverageComplete': False}]
        report = {'schemaVersion': 'teeni-product-research/1.0.0', 'productVersion': 'M1', 'dataDate': day,
                  'sources': sources, 'behavior': longitudinal({day: set()}, set()),
                  'systemIntentDemand': {}, 'instrumentation': {}, 'evaluation': evaluation}
        DailySnapshot.objects.create(product_version='M1', scene_id='488', data_date=day, core_version='2.7.0', rules_version='16.0.0', contract_version='teeni-base-detail/1.2.0', source_sha256='a' * 64, workbook_sha256='b' * 64, payload={'baseDetailSha256': 'c' * 64})
        with tempfile.TemporaryDirectory() as root:
            legacy = Path(root) / 'legacy.json'
            output = Path(root) / 'updated.json'
            _write_json(legacy, report)
            ProductResearchSnapshot.objects.create(product_version='M1', data_date=day, payload=report, source_sha256=sha256_file(legacy))
            with patch('dashboard.research._read_inputs', return_value=('M1', day, [{**sources[0], 'detailSha256': 'c' * 64}], {day: set()}, set(), {}, {})):
                updated = recompute_behavior_preserving_evaluation('input.json', legacy)
            _write_json(output, updated)
            for _ in range(2):
                call_command('import_research_snapshot', summary=str(output), sha256=sha256_file(output), apply=True, stdout=io.StringIO())
            self.assertEqual(ProductResearchSnapshot.objects.get(product_version='M1').payload, updated)
            updated['evaluation']['status'] = 'changed'
            updated['evaluationProvenance']['evaluationSha256'] = _evaluation_hash(updated)
            _write_json(output, updated)
            with self.assertRaisesRegex(CommandError, 'provenance changed'):
                call_command('import_research_snapshot', summary=str(output), sha256=sha256_file(output), apply=True, stdout=io.StringIO())

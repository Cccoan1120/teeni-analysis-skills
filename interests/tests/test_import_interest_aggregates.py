import json
from io import StringIO

from django.core.management import call_command, CommandError
from django.test import TestCase

from dashboard.models import DailySnapshot
from interest_engine.batch import analyze_batch, load_batch_manifest
from interests.models import InterestSnapshot, InterestDailyContribution
from interests.tests import test_import_interest_batch as batch_tests
from topics.models import TopicJob, TopicArtifact


class AggregatePublicationTests(TestCase):
    write_batch = batch_tests.ImportInterestBatchTests.write_batch

    def setUp(self):
        batch_tests.ImportInterestBatchTests.setUp(self)
        self.payload['packages'] = self.payload['packages'][:1]
        self.write_batch()
        contract = load_batch_manifest(self.batch)
        from interest_engine.model import LocalCandidatePassthroughEnricher
        result = analyze_batch(contract, self.root / 'analysis', enricher=LocalCandidatePassthroughEnricher())
        call_command('prepare_interest_aggregates', manifest=str(self.batch), result_manifest=str(result.manifest_path),
                     output_dir=str(self.root / 'bundle'), stdout=StringIO())
        self.package = self.root / 'bundle' / '2026-08-21' / 'package.json'
        DailySnapshot.objects.create(data_date='2026-08-21', product_version='M1', scene_id='488',
            payload={'baseDetailSha256': self.payload['packages'][0]['detailSha256']})

    def test_aggregate_publication_reconciles_and_retry_is_noop(self):
        call_command('import_interest_aggregates', package=str(self.package), stdout=StringIO())
        self.assertEqual(InterestSnapshot.objects.count(), 0)
        call_command('import_interest_aggregates', package=str(self.package), apply=True, backup_dir=str(self.root / 'backup'), stdout=StringIO())
        self.assertEqual(InterestSnapshot.objects.count(), 1)
        self.assertEqual(InterestDailyContribution.objects.count(), 1)
        self.assertEqual(list(TopicArtifact.objects.values_list('kind', flat=True)), [TopicArtifact.Kind.INTEREST_WORKBOOK])
        count = TopicJob.objects.count()
        output = StringIO()
        call_command('import_interest_aggregates', package=str(self.package), apply=True, backup_dir=str(self.root / 'backup'), stdout=output)
        self.assertEqual(json.loads(output.getvalue())['status'], 'unchanged')
        self.assertEqual(TopicJob.objects.count(), count)

    def test_tampered_bundle_and_mismatched_base_are_rejected(self):
        original = self.package.read_text()
        payload = json.loads(original)
        payload['dataDate'] = '2026-08-22'
        self.package.write_text(json.dumps(payload))
        with self.assertRaisesRegex(CommandError, 'authentication'):
            call_command('import_interest_aggregates', package=str(self.package), stdout=StringIO())
        self.package.write_text(original)
        DailySnapshot.objects.update(payload={'baseDetailSha256': 'f' * 64})
        with self.assertRaisesRegex(CommandError, 'published base'):
            call_command('import_interest_aggregates', package=str(self.package), stdout=StringIO())
        self.assertEqual(InterestSnapshot.objects.count(), 0)

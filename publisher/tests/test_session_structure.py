import csv
import tempfile
import unittest
from pathlib import Path

from publisher.session_structure import from_detail, validate_structure


class SessionStructureTests(unittest.TestCase):
    def calculate(self, lengths, texts=None, mutate=None, day='2026-09-13'):
        rows = []
        for session, length in enumerate(lengths):
            for turn in range(length):
                number = len(rows) + 1
                rows.append(dict(id=str(number), cid=f's{session}', clientId='u', source_row=number,
                                 sceneId='488', created_at=f'{day} 12:00:00',
                                 text=(texts or ['hello'])[turn % len(texts or ['hello'])]))
        if mutate:
            mutate(rows)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'detail.csv'
            with path.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['id','cid','clientId','source_row','sceneId','created_at','text'])
                writer.writeheader()
                writer.writerows(rows)
            return from_detail(path, '488', day, source_row_start=1)

    def test_counts_and_content_independence(self):
        value = self.calculate([1,1,3,5])
        self.assertEqual((value['totalTurns'],value['totalSessions'],value['multiTurnRate'],value['multiTurnAverageTurns']), (10,4,.5,4))
        self.assertEqual(value, self.calculate([1,1,3,5], ['##{startPrompt}##', 'any new opening', '']))

    def test_no_sample_and_all_single(self):
        value = self.calculate([])
        self.assertIsNone(value['averageTurns'])
        self.assertIsNone(value['multiTurnRate'])
        value = self.calculate([1,1])
        self.assertEqual(value['multiTurnRate'], 0)
        self.assertIsNone(value['multiTurnAverageTurns'])

    def test_identity_and_date_failures(self):
        mutations = [lambda r:r[1].update(id=r[0]['id']),
                     lambda r:r[1].update(clientId='another'),
                     lambda r:r[1].update(created_at='2026-09-14 00:00:00'),
                     lambda r:r[1].update(source_row=9),
                     lambda r:r[1].update(sceneId='904')]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.calculate([2], mutate=mutate)

    def test_validation_rejects_fake_rate_and_impossible_counts(self):
        for key, value in [('multiTurnRate',.9),('totalTurns',99),('fivePlusSessions',3)]:
            metrics = self.calculate([1,2,5])
            metrics[key] = value
            with self.assertRaises(ValueError):
                validate_structure(metrics)

    def test_five_plus_and_daily_fragment(self):
        metrics = self.calculate([2, 100])
        self.assertEqual(metrics['fivePlusSessions'], 1)
        metrics['fivePlusSessions'] = 0
        metrics['fivePlusRate'] = 0
        with self.assertRaises(ValueError):
            validate_structure(metrics)
        first_day = self.calculate([2], day='2026-09-12')
        second_day = self.calculate([3], day='2026-09-13')
        self.assertEqual((first_day['averageTurns'], second_day['averageTurns']), (2, 3))
        self.assertEqual((first_day['fivePlusSessions'], second_day['fivePlusSessions']), (0, 0))

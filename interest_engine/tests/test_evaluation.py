import csv
import json
import tempfile
import unittest
from pathlib import Path

from interest_engine.evaluation import evaluate, prepare


class SemanticEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.detail = self.root / "detail.csv"
        rows = [{"source_row": "1", "clientId": "private-user", "cid": "private-session", "text": "奥特曼", "ai_text": "好的",
                 "route": "有效内容", "behavior": "未识别行为", "entity_names": "奥特曼", "entity_signals": "主动发起",
                 "preference_polarities": "neutral", "match_bases": "explicit_alias"},
                {"source_row": "2", "clientId": "private-user", "cid": "private-session", "text": "神秘新作品", "ai_text": "好的",
                 "route": "有效内容", "behavior": "未识别行为", "entity_names": "", "entity_signals": "",
                 "preference_polarities": "", "match_bases": ""}]
        with self.detail.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.output = self.root / "review"
        prepare(self.detail, self.output, 2)
        self.labels = self.output / "semantic-review.private.csv"
        self.predictions = self.output / "semantic-predictions.private.json"

    def rewrite(self, labels):
        with self.labels.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(labels[0]))
            writer.writeheader()
            writer.writerows(labels)

    def test_pending_annotations_never_claim_accuracy(self):
        result = evaluate(self.labels, self.predictions)
        self.assertEqual(result["humanReviewedSamples"], 0)
        self.assertIsNone(result["entityPrecision"])
        self.assertFalse(result["reviewComplete"])
        self.assertNotIn("private-user", json.dumps(result))
        with self.assertRaisesRegex(ValueError, "already exists"):
            prepare(self.detail, self.output)

    def test_model_labels_rejected_and_missed_entity_counts_in_recall(self):
        with self.labels.open(encoding="utf-8-sig", newline="") as stream:
            labels = list(csv.DictReader(stream))
        for label in labels:
            label.update({"label_source": "model", "annotator_1": "test-human-a", "annotator_2": "test-human-b", "review_status": "reviewed"})
        self.rewrite(labels)
        with self.assertRaisesRegex(ValueError, "human provenance"):
            evaluate(self.labels, self.predictions)
        for label in labels:
            label.update({"label_source": "human", "expected_route": "有效内容", "expected_behavior": "未识别行为",
                          "expected_entity_names": label["user_query"], "expected_entity_signals": "主动发起", "expected_preference_polarities": "neutral"})
        self.rewrite(labels)
        result = evaluate(self.labels, self.predictions)
        self.assertEqual(result["entityPrecision"], 1)
        self.assertEqual(result["entityRecall"], 0.5)
        self.assertTrue(result["hitAndMissReviewed"])
        self.assertTrue(result["reviewComplete"])

    def test_source_edits_rejected(self):
        with self.labels.open(encoding="utf-8-sig", newline="") as stream:
            labels = list(csv.DictReader(stream))
        labels[0]["user_query"] = "changed"
        self.rewrite(labels)
        with self.assertRaisesRegex(ValueError, "identity changed"):
            evaluate(self.labels, self.predictions)

    def test_ai_review_reports_agreement_without_human_accuracy(self):
        with self.labels.open(encoding="utf-8-sig", newline="") as stream:
            labels = list(csv.DictReader(stream))
        for label in labels:
            label.update(label_source='ai', annotator_1='Codex', review_status='ai_reviewed', expected_route='有效内容', expected_behavior='未识别行为',
                         expected_entity_names=label['user_query'], expected_entity_signals='主动发起', expected_preference_polarities='neutral')
        self.rewrite(labels)
        result = evaluate(self.labels, self.predictions, 'ai')
        self.assertEqual(result['aiReviewedSamples'], 2)
        self.assertEqual(result['humanReviewedSamples'], 0)
        self.assertEqual(result['aiEntityRecall'], 0.5)
        self.assertIsNone(result['entityRecall'])
        self.assertTrue(all(value is None for value in result['sampleAccuracy'].values()))
        human = evaluate(self.labels, self.predictions)
        self.assertEqual(human['humanReviewedSamples'], 0)

    def test_entity_labels_follow_identity_not_serialization_order(self):
        with self.labels.open(encoding='utf-8-sig', newline='') as stream:
            labels = list(csv.DictReader(stream))
        package = json.loads(self.predictions.read_text(encoding='utf-8'))
        for label in labels:
            label.update(label_source='ai', annotator_1='Codex', review_status='ai_reviewed', expected_route='有效内容', expected_behavior='偏好表达',
                         expected_entity_names='奥特曼|小猪佩奇', expected_entity_signals='主动发起|主动延续', expected_preference_polarities='positive|negative')
            package['samples'][label['sample_id']].update(route='有效内容', behavior='偏好表达', entity_names='小猪佩奇|奥特曼',
                                                         entity_signals='主动延续|主动发起', preference_polarities='negative|positive')
        self.predictions.write_text(json.dumps(package), encoding='utf-8')
        self.rewrite(labels)
        result = evaluate(self.labels, self.predictions, 'ai')
        self.assertTrue(all(value == 1 for value in result['aiAgreement'].values()))
        labels[0]['expected_preference_polarities'] = 'negative|positive'
        self.rewrite(labels)
        self.assertEqual(evaluate(self.labels, self.predictions, 'ai')['aiAgreement']['preference_polarities'], 0.5)

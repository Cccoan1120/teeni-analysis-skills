import json
import tempfile
from datetime import date
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from dashboard.insights import create_generated_insight, publish_revision
from dashboard.models import DailySnapshot, ManagementInsight
from dashboard.tests.test_insights import create_snapshot


class RefreshAutomaticInsightsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("refresh@example.com")
        create_snapshot("2026-09-05", 100, 10, 5, .5, .7, 10, "M2")
        create_snapshot("2026-09-06", 110, 11, 6, .5, .7, 10, "M2")

    def create(self, *, actor=None, origin=ManagementInsight.Origin.AUTOMATIC, outdated=True):
        insight = create_generated_insight(date(2026, 9, 6), date(2026, 9, 6), actor, origin, "M2")
        if outdated:
            insight.revisions.update(summary="旧自动摘要。")
        return insight

    def run_command(self, root, apply=False):
        output = StringIO()
        call_command("refresh_automatic_insights", output_dir=root, apply=apply, stdout=output)
        return json.loads(output.getvalue())

    def test_dry_run_preserves_revision_and_apply_appends_once(self):
        changed = self.create()
        original = changed.revisions.get(revision=1)
        unchanged = self.create(outdated=False)
        with tempfile.TemporaryDirectory() as root:
            preview = self.run_command(root)
            self.assertEqual((preview["wouldRefresh"], preview["refreshed"], preview["unchanged"]), (1, 0, 1))
            changed.refresh_from_db()
            self.assertEqual(changed.current_revision, 1)
            plan = json.loads(Path(preview["backupAndPlan"]).read_text(encoding="utf-8"))
            self.assertEqual(plan["changes"][0]["before"]["summary"], "旧自动摘要。")
            applied = self.run_command(root, apply=True)
            self.assertEqual(applied["refreshed"], 1)
            changed.refresh_from_db()
            original.refresh_from_db()
            self.assertEqual((changed.current_revision, changed.status), (2, ManagementInsight.Status.DRAFT))
            self.assertEqual(original.summary, "旧自动摘要。")
            self.assertEqual(changed.revisions.count(), 2)
            self.assertIsNone(changed.revisions.get(revision=2).created_by)
            repeated = self.run_command(root, apply=True)
            self.assertEqual(repeated["refreshed"], 0)
            unchanged.refresh_from_db()
            self.assertEqual(unchanged.current_revision, 1)

    def test_manual_and_published_and_human_revision_are_excluded(self):
        manual = self.create(origin=ManagementInsight.Origin.MANUAL)
        actor = self.create(actor=self.user)
        published = self.create(origin=ManagementInsight.Origin.INITIAL)
        publish_revision(published.pk, 1, self.user)
        automatic_published = self.create()
        publish_revision(automatic_published.pk, 1, self.user)
        reviewed = self.create()
        reviewed.revisions.update(created_by=self.user)
        with tempfile.TemporaryDirectory() as root:
            result = self.run_command(root, apply=True)
        self.assertEqual(result["refreshed"], 0)
        self.assertEqual(result["skipped"], 1)
        for insight in (manual, actor, published, automatic_published, reviewed):
            insight.refresh_from_db()
            self.assertEqual(insight.current_revision, 1)
            self.assertEqual(insight.revisions.get(revision=1).summary, "旧自动摘要。")

    def test_single_contract_day_is_not_reported_as_unchanged(self):
        insight = create_generated_insight(date(2026, 9, 5), date(2026, 9, 5), None,
                                           ManagementInsight.Origin.AUTOMATIC, "M2")
        current = insight.revisions.get(revision=1)
        self.assertIn("暂无同定义且有样本的可比日期", current.summary)
        self.assertNotIn("持平", current.summary)
        self.assertFalse(current.metric_snapshot["comparisonAvailable"])

    def test_missing_structure_is_skipped_without_destroying_old_revision(self):
        insight = self.create()
        snapshot = DailySnapshot.objects.get(product_version='M2', data_date='2026-09-06')
        snapshot.payload['metrics'].pop('sessionStructure')
        snapshot.save(update_fields=['payload'])
        with tempfile.TemporaryDirectory() as root:
            result = self.run_command(root, apply=True)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['refreshed'], 0)
        insight.refresh_from_db()
        self.assertEqual(insight.current_revision, 1)
        self.assertEqual(insight.revisions.get(revision=1).summary, '旧自动摘要。')

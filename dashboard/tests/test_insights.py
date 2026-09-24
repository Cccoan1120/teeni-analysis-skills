import json
from datetime import date

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from dashboard.insights import create_generated_insight, publish_revision, serialize_insight, ensure_daily_draft
from dashboard.models import DailySnapshot, ManagementInsight, ManagementInsightRevision


def snapshot_payload(
    data_date: str,
    turns: int,
    sessions: int,
    users: int,
    opening: float,
    participation: float,
    depth: float,
    product_version: str = "M1",
) -> dict:
    scene_id = "488" if product_version == "M1" else "904"
    payload = {
        "schemaVersion": "teeni-dashboard-snapshot/1.1.0" if product_version == "M1" else "teeni-dashboard-snapshot/1.2.0",
        "dataDate": data_date,
        "generatedAt": f"{data_date}T23:59:00+08:00",
        "sceneId": scene_id,
        "coreVersion": "2.5.0",
        "rulesVersion": "11.0.0" if product_version == "M1" else "12.0.0",
        "contractVersion": "teeni-base-detail/1.2.0",
        "sourceSha256": ("a" if data_date.endswith("28") else "c") * 64,
        "workbookSha256": ("b" if data_date.endswith("28") else "d") * 64,
        "metrics": {
            "sessionStructure": {"schemaVersion": "teeni-session-structure/1.0.0", "totalTurns": turns,
                "totalSessions": sessions, "singleTurnSessions": sessions // 2, "multiTurnSessions": sessions - sessions // 2,
                "multiTurnTurns": turns - sessions // 2, "fivePlusSessions": sessions // 4,
                "averageTurns": turns / sessions if sessions else None,
                "multiTurnRate": (sessions - sessions // 2) / sessions if sessions else None,
                "multiTurnAverageTurns": (turns - sessions // 2) / (sessions - sessions // 2) if sessions else None,
                "fivePlusRate": (sessions // 4) / sessions if sessions else None},
            "volume": {"turns": turns, "sessions": sessions, "users": users},
            "participation": {"sessions": round(sessions * participation), "eligibleSessions": sessions, "rate": participation},
            "opening": {"exposures": 1000, "opened": round(1000 * opening), "rate": opening},
            "depth": {"rawAverage": depth, "postTemplateAverage": depth + 2, "netValidAverage": depth + 3},
        },
    }
    if product_version == "M2":
        payload["productVersion"] = "M2"
    return payload


def create_snapshot(
    data_date: str,
    turns: int,
    sessions: int,
    users: int,
    opening: float,
    participation: float,
    depth: float,
    product_version: str = "M1",
) -> DailySnapshot:
    payload = snapshot_payload(
        data_date,
        turns,
        sessions,
        users,
        opening,
        participation,
        depth,
        product_version,
    )
    return DailySnapshot.objects.create(
        data_date=data_date,
        product_version=product_version,
        scene_id="488" if product_version == "M1" else "904",
        core_version="2.5.0",
        rules_version="11.0.0" if product_version == "M1" else "12.0.0",
        contract_version="teeni-base-detail/1.2.0",
        source_sha256=payload["sourceSha256"],
        workbook_sha256=payload["workbookSha256"],
        payload=payload,
    )


class ManagementInsightApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="member@example.com",
            email="member@example.com",
            password="test-password-42",
        )
        self.client.force_login(self.user)
        create_snapshot("2026-08-28", 1000, 100, 50, 0.50, 0.70, 10.0)
        create_snapshot("2026-08-29", 900, 100, 52, 0.48, 0.68, 9.0)
        create_snapshot("2026-08-30", 800, 100, 51, 0.45, 0.65, 8.0)

    def create_range(self, product_version: str = "M1"):
        return self.client.post(
            reverse("dashboard:insights"),
            data=json.dumps(
                {
                    "startDate": "2026-08-28",
                    "endDate": "2026-08-30",
                    "productVersion": product_version,
                }
            ),
            content_type="application/json",
        )

    def test_login_required(self):
        client = Client()
        self.assertEqual(client.get(reverse("dashboard:insights")).status_code, 302)

    def test_create_range_generates_aggregate_draft_and_shapley(self):
        response = self.create_range()
        self.assertEqual(response.status_code, 201)
        insight = response.json()["insight"]
        self.assertEqual(insight["status"], "draft")
        self.assertEqual(insight["productVersion"], "M1")
        self.assertEqual(
            insight["current"]["metricSnapshot"]["contract"]["productVersion"],
            "M1",
        )
        self.assertEqual(insight["currentRevision"], 1)
        contribution = insight["current"]["metricSnapshot"]["contributions"]
        self.assertAlmostEqual(contribution["depthShare"], 1.0)
        self.assertEqual(len(insight["current"]["sourceHashes"]), 3)

    def test_base_rule_changes_do_not_break_structure_comparison(self):
        last = DailySnapshot.objects.get(data_date="2026-08-30")
        last.rules_version = "10.0.0"
        last.save(update_fields=["rules_version"])
        response = self.create_range()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['insight']['current']['metricSnapshot']['contract']['sessionStructureVersion'], 'teeni-session-structure/1.0.0')

    def test_missing_structure_cannot_use_legacy_averages(self):
        last = DailySnapshot.objects.get(data_date='2026-08-30')
        last.payload['metrics'].pop('sessionStructure')
        last.save(update_fields=['payload'])
        self.assertEqual(self.create_range().status_code, 400)
        self.assertIsNone(ensure_daily_draft(last))

    def test_daily_comparison_skips_missing_and_empty_structure_dates(self):
        middle = DailySnapshot.objects.get(data_date='2026-08-29')
        last = DailySnapshot.objects.get(data_date='2026-08-30')
        for empty in (False, True):
            with self.subTest(empty=empty):
                middle.payload = snapshot_payload('2026-08-29', 0, 0, 0, 0, 0, 0)
                if not empty:
                    middle.payload['metrics'].pop('sessionStructure')
                middle.save(update_fields=['payload'])
                result = serialize_insight(ensure_daily_draft(last))['current']
                self.assertEqual(result['metricSnapshot']['comparisonLabel'], '较 2026-08-28')
                self.assertEqual(set(result['sourceHashes']), {'2026-08-28', '2026-08-30'})

    def test_automatic_revision_preserves_history_and_authored_changes(self):
        last = DailySnapshot.objects.get(data_date='2026-08-30')
        insight = ensure_daily_draft(last)
        initial = insight.revisions.get(revision=1)
        initial.metric_snapshot = {'schemaVersion': 'teeni-management-insight-metrics/1.1.0'}
        initial.save(update_fields=['metric_snapshot'])
        insight = ensure_daily_draft(last)
        self.assertEqual(insight.current_revision, 2)
        self.assertEqual(ensure_daily_draft(last).current_revision, 2)
        result = serialize_insight(insight, include_history=True)
        self.assertEqual(next(row for row in result['history'] if row['revision'] == 1)['definitionStatus'], 'historical_definition')
        current = insight.revisions.get(revision=2)
        current.created_by = self.user
        current.save(update_fields=['created_by'])
        last.workbook_sha256 = 'e' * 64
        last.save(update_fields=['workbook_sha256'])
        self.assertEqual(ensure_daily_draft(last).current_revision, 2)

    def test_structure_change_is_stale_without_workbook_change(self):
        insight = create_generated_insight(date(2026, 8, 30), date(2026, 8, 30), self.user)
        previous = DailySnapshot.objects.get(data_date='2026-08-29')
        previous.payload['metrics']['sessionStructure']['fivePlusSessions'] += 1
        previous.save(update_fields=['payload'])
        self.assertTrue(serialize_insight(insight)['current']['sourceStale'])

    def test_empty_dates_produce_null_contributions(self):
        create_snapshot('2026-09-01', 0, 0, 0, 0, 0, 0)
        insight = create_generated_insight(date(2026, 9, 1), date(2026, 9, 1), self.user)
        result = serialize_insight(insight)['current']
        self.assertIsNone(result['metricSnapshot']['points'][0]['averageTurns'])
        self.assertIsNone(result['metricSnapshot']['contributions']['depth'])
        self.assertNotIn('真实参与', json.dumps(result, ensure_ascii=False))

    def test_growth_with_negative_depth_preserves_direction(self):
        create_snapshot("2026-09-05", 28683, 3120, 434, .57, .70, 28683 / 3120, "M2")
        create_snapshot("2026-09-06", 36295, 3995, 606, .58, .71, 36295 / 3995, "M2")
        response = self.client.post(reverse("dashboard:insights"), data=json.dumps({
            "startDate": "2026-09-05", "endDate": "2026-09-06", "productVersion": "M2",
        }), content_type="application/json")
        self.assertEqual(response.status_code, 201)
        revision = response.json()["insight"]["current"]
        self.assertIn("抵消", revision["summary"])
        self.assertIn("-5.06%", revision["summary"])
        self.assertNotIn("降至", revision["facts"][0]["note"])

    def test_zero_total_change_does_not_invent_contribution_percentage(self):
        create_snapshot("2026-09-05", 100, 10, 5, .5, .7, 10, "M2")
        create_snapshot("2026-09-06", 100, 20, 5, .5, .7, 5, "M2")
        response = self.client.post(reverse("dashboard:insights"), data=json.dumps({
            "startDate": "2026-09-05", "endDate": "2026-09-06", "productVersion": "M2",
        }), content_type="application/json")
        self.assertEqual(response.status_code, 201)
        self.assertIn("不计算贡献占比", response.json()["insight"]["current"]["summary"])

    def test_revision_conflict_and_privacy_rejection(self):
        insight = self.create_range().json()["insight"]
        current = insight["current"]
        valid = {
            "baseRevision": 1,
            "title": current["title"],
            "summary": "更新后的聚合结论。",
            "facts": current["facts"],
            "mechanisms": current["mechanisms"],
            "associations": current["associations"],
            "hypotheses": current["hypotheses"],
            "actions": current["actions"],
        }
        url = reverse("dashboard:insight-detail", args=[insight["id"]])
        saved = self.client.put(url, data=json.dumps(valid), content_type="application/json")
        self.assertEqual(saved.status_code, 200)
        conflict = self.client.put(url, data=json.dumps(valid), content_type="application/json")
        self.assertEqual(conflict.status_code, 409)

        invalid = {**valid, "baseRevision": 2, "summary": "联系 manager@example.com 并查看 cid。"}
        rejected = self.client.put(url, data=json.dumps(invalid), content_type="application/json")
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(ManagementInsightRevision.objects.filter(insight_id=insight["id"]).count(), 2)

    def test_publish_withdraw_and_republish_historical_revision(self):
        insight = self.create_range().json()["insight"]
        publish_url = reverse("dashboard:insight-publish", args=[insight["id"]])
        published = self.client.post(publish_url, data=json.dumps({"revision": 1}), content_type="application/json")
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()["insight"]["publishedRevision"], 1)

        withdrawn = self.client.post(reverse("dashboard:insight-withdraw", args=[insight["id"]]))
        self.assertEqual(withdrawn.status_code, 200)
        self.assertEqual(withdrawn.json()["insight"]["status"], "withdrawn")

        republished = self.client.post(publish_url, data=json.dumps({"revision": 1}), content_type="application/json")
        self.assertEqual(republished.status_code, 200)
        self.assertEqual(republished.json()["insight"]["status"], "published")

    def test_source_hash_change_marks_published_revision_stale(self):
        insight = create_generated_insight(date(2026, 8, 28), date(2026, 8, 30), self.user)
        insight = publish_revision(insight.id, 1, self.user)
        snapshot = DailySnapshot.objects.get(data_date="2026-08-30")
        snapshot.workbook_sha256 = "f" * 64
        snapshot.save(update_fields=["workbook_sha256"])
        self.assertTrue(serialize_insight(insight)["published"]["sourceStale"])

    def test_m1_and_m2_insights_are_isolated_for_same_dates(self):
        create_snapshot("2026-08-28", 2000, 120, 60, 0.60, 0.80, 12.0, "M2")
        create_snapshot("2026-08-29", 1900, 118, 61, 0.58, 0.78, 11.0, "M2")
        create_snapshot("2026-08-30", 1800, 116, 62, 0.56, 0.76, 10.0, "M2")

        m1 = self.create_range("M1").json()["insight"]
        m2 = self.create_range("M2").json()["insight"]
        m1_rows = self.client.get(reverse("dashboard:insights"), {"productVersion": "M1"}).json()["items"]
        m2_rows = self.client.get(reverse("dashboard:insights"), {"productVersion": "M2"}).json()["items"]

        self.assertEqual(m1["current"]["metricSnapshot"]["points"][0]["turns"], 1000)
        self.assertEqual(m2["current"]["metricSnapshot"]["points"][0]["turns"], 2000)
        self.assertEqual({item["productVersion"] for item in m1_rows}, {"M1"})
        self.assertEqual({item["productVersion"] for item in m2_rows}, {"M2"})

    def test_m2_snapshot_change_does_not_mark_m1_revision_stale(self):
        create_snapshot("2026-08-28", 2000, 120, 60, 0.60, 0.80, 12.0, "M2")
        create_snapshot("2026-08-29", 1900, 118, 61, 0.58, 0.78, 11.0, "M2")
        create_snapshot("2026-08-30", 1800, 116, 62, 0.56, 0.76, 10.0, "M2")
        insight = create_generated_insight(date(2026, 8, 28), date(2026, 8, 30), self.user)
        insight = publish_revision(insight.id, 1, self.user)

        m2 = DailySnapshot.objects.get(data_date="2026-08-30", product_version="M2")
        m2.workbook_sha256 = "f" * 64
        m2.save(update_fields=["workbook_sha256"])

        self.assertFalse(serialize_insight(insight)["published"]["sourceStale"])

    def test_write_api_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(
            reverse("dashboard:insights"),
            data=json.dumps({"startDate": "2026-08-28", "endDate": "2026-08-30"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

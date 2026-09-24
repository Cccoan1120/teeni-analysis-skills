import csv
import gzip
import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from interest_engine.analysis import _key
from interest_engine.classifier import InterestSignal, QueryRoute
from interest_engine.contracts import sha256_file
from interests.models import InterestDailyContribution
from interests.preferences import aggregate_preferences, build_contribution, save_contribution


@override_settings(TEENI_INTEREST_IDENTITY_KEY="preference-test-identity")
class InterestPreferenceTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps({
            "schemaVersion": "teeni-interest-registry/1.1.0", "registryVersion": "preferences-test",
            "entities": [{"id": "IE1", "canonicalName": "奥特曼", "entityType": "作品",
                          "entitySubtype": "特摄", "broadTopic": "影视动漫与角色",
                          "matchRules": [{"value": "奥特曼", "mode": "substring", "policy": "auto"}]}],
        }, ensure_ascii=False), encoding="utf-8")
        user = get_user_model().objects.create_user("preferences@example.com", password="test-password-42")
        self.client.force_login(user)

    def contribution(self, data_date, signals, *, user="same-user", session="same-session", age=6, gender="男", product_version="M1"):
        base, safe = self.root / f"{data_date}-base.csv", self.root / f"{data_date}-safe.csv"
        rows = [{"source_row": str(index), "clientId": user, "cid": session, "sceneId": "488" if product_version == "M1" else "904",
                 "created_at": f"{data_date}T23:00:00+08:00", "profile_age": str(age),
                 "profile_gender": gender, "age_status": "正常", "gender_status": "正常",
                 "city_normalized": "北京", "city_status": "正常"}
                for index in range(1, len(signals) + 1)]
        with base.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        digest = sha256_file(base)
        classified = [{"source_row": row["source_row"], "user_key": _key("user", user, digest),
                       "session_key": _key("session", session, digest), "entity_ids": "IE1",
                       "route": QueryRoute.VALID_CONTENT.value, "interest_signal": signal.value}
                      for row, signal in zip(rows, signals)]
        with safe.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(classified[0]))
            writer.writeheader()
            writer.writerows(classified)
        initiated, continued = InterestSignal.INITIATION in signals, InterestSignal.CONTINUATION in signals
        snapshot = {"dataDate": data_date, "productVersion": product_version, "sceneId": "488" if product_version == "M1" else "904", "sourceSha256": digest, "registrySha256": sha256_file(self.registry),
                    "totals": {"inputQueries": len(rows)}, "ipRollups": [{
                        "id": "IE1", "name": "奥特曼", "entityType": "作品", "entitySubtype": "特摄",
                        "rollupKind": "standalone", "activeInterestUsers": int(initiated or continued),
                        "mentionUsers": 1, "initiatorUsers": int(initiated), "continuationUsers": int(continued),
                        "queries": len(rows), "sessions": 1, "deepSessions": int(len(rows) >= 3),
                    }]}
        payload = build_contribution(base, safe, self.registry, snapshot)
        return save_contribution(snapshot, payload), snapshot, payload

    def test_safety_only_revision_keeps_period_union_and_original_versions(self):
        records = []
        for day, core, rules in [('2026-09-07', '2.6.0', '14.0.0'), ('2026-09-08', '2.6.1', '15.0.0')]:
            record, _, payload = self.contribution(day, [InterestSignal.INITIATION])
            payload['compatibility'] = {'engineVersion': 'teeni-interest-engine/1.5.0',
                                        'detailSchema': 'teeni-interest-detail/1.2.0',
                                        'baseCoreVersion': core, 'baseRulesVersion': rules}
            record.payload = gzip.compress(json.dumps(payload).encode())
            record.save()
            records.append(record)
        self.assertEqual(aggregate_preferences(records)['totals']['activeInterestUsers'], 1)
        response = self.client.get('/api/interests/preferences/', {'period': 'month', 'month': '2026-09', 'productVersion': 'M1', 'dimensionSet': 'all'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['dates'], ['2026-09-07', '2026-09-08'])
        self.assertEqual(response.json()['excludedIncompatibleDates'], [])
        records[-1].refresh_from_db()
        self.assertEqual(json.loads(gzip.decompress(bytes(records[-1].payload)))['compatibility']['baseRulesVersion'], '15.0.0')

    def test_cross_day_user_union_and_session_deep_chat(self):
        first, _, payload = self.contribution("2026-08-31", [InterestSignal.INITIATION])
        second, _, _ = self.contribution("2026-09-01", [InterestSignal.CONTINUATION, InterestSignal.CONTINUATION])
        result = aggregate_preferences([first, second])
        self.assertEqual(result["totals"]["groupUsers"], 1)
        self.assertEqual(result["totals"]["activeInterestUsers"], 1)
        ip = result["ips"][0]
        self.assertEqual((ip["initiatorUsers"], ip["continuationUsers"], ip["activeInterestUsers"]), (1, 1, 1))
        self.assertEqual((ip["queries"], ip["sessions"], ip["deepSessions"], ip["deepChatRate"]), (3, 1, 1, 1))
        private = json.dumps(payload)
        self.assertNotIn("same-user", private)
        self.assertNotIn("same-session", private)

    def test_same_date_replacement_does_not_accumulate_old_users(self):
        old, _, _ = self.contribution("2026-09-01", [InterestSignal.INITIATION])
        new, _, _ = self.contribution("2026-09-01", [InterestSignal.CONTINUATION] * 2, user="replacement-user")
        self.assertEqual(old.pk, new.pk)
        self.assertEqual(InterestDailyContribution.objects.count(), 1)
        result = aggregate_preferences(InterestDailyContribution.objects.all())
        self.assertEqual(result["totals"]["activeInterestUsers"], 1)
        self.assertEqual(result["ips"][0]["queries"], 2)
        self.assertEqual(result["ips"][0]["initiatorUsers"], 0)

    def test_day_month_all_and_dimension_api(self):
        self.contribution("2026-08-31", [InterestSignal.INITIATION])
        self.contribution("2026-09-01", [InterestSignal.CONTINUATION] * 2)
        self.contribution("2026-09-02", [InterestSignal.INITIATION], user="other-user", session="other-session", age=7, gender="女")
        for params, days, users in (({"period": "day", "date": "2026-09-01"}, 1, 1),
                                    ({"period": "month", "month": "2026-09"}, 2, 2),
                                    ({"period": "all"}, 3, 2)):
            with self.subTest(params=params):
                response = self.client.get("/api/interests/preferences/", params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["coveredDays"], days)
                self.assertEqual(response.json()["totals"]["activeInterestUsers"], users)
                self.assertNotIn("same-user", response.content.decode())
        response = self.client.get("/api/interests/preferences/", {
            "period": "all", "dimensionSet": "age_gender", "groupKey": "6~male"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["selectedGroup"]["eligibleUsers"], 1)
        self.assertEqual(response.json()["ips"][0]["queries"], 3)
        self.assertEqual(response.json()["ips"][0]["sampleStatus"], "样本不足")
        self.assertEqual(self.client.get("/api/interests/preferences/", {"period": "quarter"}).status_code, 400)
        self.assertEqual(self.client.get("/api/interests/preferences/", {"dimensionSet": "city"}).status_code, 400)

    def test_versions_coexist_and_period_preferences_do_not_mix(self):
        m1, _, _ = self.contribution("2026-09-01", [InterestSignal.INITIATION])
        m2, _, _ = self.contribution("2026-09-01", [InterestSignal.CONTINUATION] * 2, product_version="M2")
        self.assertNotEqual(m1.pk, m2.pk)
        self.assertEqual(InterestDailyContribution.objects.count(), 2)
        for period in ("day", "month", "all"):
            for version, queries in (("M1", 1), ("M2", 2)):
                response = self.client.get("/api/interests/preferences/", {
                    "period": period, "date": "2026-09-01", "month": "2026-09", "productVersion": version,
                })
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()["productVersion"], version)
                self.assertEqual(response.json()["coveredDays"], 1)
                self.assertEqual(response.json()["ips"][0]["queries"], queries)
        with self.assertRaisesRegex(ValueError, "across product versions"):
            aggregate_preferences([m1, m2])
        self.assertEqual(self.client.get("/api/interests/preferences/", {"productVersion": "M3"}).status_code, 400)

    def test_identity_key_change_requires_rebuild(self):
        self.contribution("2026-09-01", [InterestSignal.INITIATION])
        with override_settings(TEENI_INTEREST_IDENTITY_KEY="changed-preference-key"):
            response = self.client.get("/api/interests/preferences/", {"period": "all"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "preference_identity_changed")

    def test_cached_response_changes_when_day_is_replaced(self):
        self.contribution("2026-09-01", [InterestSignal.INITIATION])
        self.assertEqual(self.client.get("/api/interests/preferences/", {"period": "all"}).json()["ips"][0]["queries"], 1)
        self.contribution("2026-09-01", [InterestSignal.CONTINUATION] * 3)
        self.assertEqual(self.client.get("/api/interests/preferences/", {"period": "all"}).json()["ips"][0]["queries"], 3)

    def test_latest_profile_assigns_user_to_one_period_group(self):
        first, _, _ = self.contribution("2026-08-31", [InterestSignal.INITIATION], age=6)
        second, _, _ = self.contribution("2026-09-01", [InterestSignal.CONTINUATION], age=7)
        result = aggregate_preferences([first, second], dimension_set="age_gender_region")
        self.assertEqual([row["id"] for row in result["groups"]], ["7~male~11"])
        self.assertEqual(result["ips"][0]["queries"], 2)

    def test_incompatible_engine_or_registry_is_rejected(self):
        first, _, _ = self.contribution("2026-08-31", [InterestSignal.INITIATION])
        second, _, payload = self.contribution("2026-09-01", [InterestSignal.CONTINUATION])
        payload["compatibility"]["engineVersion"] = "different-engine"
        second.payload = gzip.compress(json.dumps(payload).encode())
        second.save()
        with self.assertRaisesRegex(ValueError, "definitions differ"):
            aggregate_preferences([first, second])
        response = self.client.get("/api/interests/preferences/", {"period": "all"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dates"], ["2026-09-01"])
        self.assertEqual(response.json()["excludedIncompatibleDates"], ["2026-08-31"])
        self.assertEqual(response.json()["totals"]["activeInterestUsers"], 1)
        self.assertEqual(response.json()["ips"][0]["queries"], 1)
        older = self.client.get("/api/interests/preferences/", {"period": "all", "definitionDate": "2026-08-31"})
        self.assertEqual(older.status_code, 200)
        self.assertEqual(older.json()["dates"], ["2026-08-31"])
        self.assertEqual(older.json()["missingDates"], [])
        self.assertEqual(self.client.get("/api/interests/preferences/", {"period": "all", "definitionDate": "2026-08-30"}).status_code, 400)

    def test_ip_sample_status_uses_ip_users_not_population(self):
        records = []
        for index in range(31):
            record, _, _ = self.contribution("2026-09-01", [InterestSignal.INITIATION], user=f"user-{index}")
            payload = json.loads(gzip.decompress(bytes(record.payload)))
            if index:
                payload["hits"] = []
            record.payload = gzip.compress(json.dumps(payload).encode())
            records.append(record)
        result = aggregate_preferences(records)
        self.assertEqual(result["totals"]["eligibleUsers"], 31)
        self.assertEqual(result["ips"][0]["sampleUsers"], 1)
        self.assertEqual(result["ips"][0]["coverageSampleUsers"], 31)
        self.assertEqual(result["ips"][0]["sampleStatus"], "样本不足")

    def test_age_groups_sort_numerically_and_default_to_largest_sample(self):
        first, _, _ = self.contribution("2026-08-30", [InterestSignal.INITIATION], age=10, user="older-user")
        second, _, _ = self.contribution("2026-08-31", [InterestSignal.INITIATION], age=2, user="younger-user")
        third, _, _ = self.contribution("2026-09-01", [InterestSignal.INITIATION], age=2, user="another-younger-user")
        result = aggregate_preferences([first, second, third], dimension_set="age")
        self.assertEqual([group["id"] for group in result["groups"]], ["2", "10"])
        self.assertEqual(result["selectedGroup"]["id"], "2")

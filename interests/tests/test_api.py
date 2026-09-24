import json
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from interests.models import (
    InterestBackfillRequest,
    InterestCandidate,
    InterestEntity,
    InterestSegmentSnapshot,
    InterestSnapshot,
)
from interests.registry import ensure_seed_registry, review_candidate
from topics.models import TopicJob


class InterestApiTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user("viewer@example.com", password="a-strong-password-42")
        self.staff = user_model.objects.create_user("reviewer@example.com", password="a-strong-password-42", is_staff=True)
        self.job = TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-25",
            original_name="scene488.csv",
            expected_size=1,
            runtime_state={
                "interestCandidateStaging": {
                    "dates": [f"2026-08-{day:02d}" for day in range(19, 26)],
                    "candidateDegraded": False,
                    "resultManifestSha256": "d" * 64,
                },
            },
        )
        self.candidate = InterestCandidate.objects.create(
            job=self.job,
            data_date="2026-08-25",
            phrase="海底新奇梗",
            normalized_phrase="海底新奇梗",
            query_count=12,
            users=7,
            sessions=9,
            suggested_name="海底新奇梗",
            suggested_type="网络热梗",
            suggested_subtype="网络句式",
            suggested_aliases=[],
            model_confidence="中",
            model_recommended=True,
        )

    def test_candidates_and_review_are_version_scoped(self):
        m2 = TopicJob.objects.create(
            created_by=self.user, data_date=self.job.data_date, original_name="m2.csv", expected_size=1, product_version="M2",
        )
        candidate = InterestCandidate.objects.create(
            job=m2, data_date=m2.data_date, phrase="M2候选", normalized_phrase="m2候选", query_count=3, users=2, sessions=2,
        )
        self.client.force_login(self.staff)
        self.assertEqual([row["id"] for row in self.client.get(reverse("interests:candidates")).json()["items"]], [str(self.candidate.id)])
        response = self.client.get(reverse("interests:candidates"), {"productVersion": "M2"})
        self.assertEqual([row["id"] for row in response.json()["items"]], [str(candidate.id)])
        endpoint = reverse("interests:review", args=[candidate.id])
        self.assertEqual(self.client.post(endpoint, data=json.dumps({"action": "reject"}), content_type="application/json").status_code, 404)
        self.assertEqual(self.client.post(endpoint + "?productVersion=M2", data=json.dumps({"action": "reject"}), content_type="application/json").status_code, 200)
        for endpoint in ("/api/interests/", "/api/interests/segments/", "/api/interests/segments/groups/", "/api/interests/candidates/"):
            self.assertEqual(self.client.get(endpoint, {"productVersion": "M3"}).status_code, 400)

    def test_interest_apis_require_login_and_review_requires_staff(self):
        anonymous = self.client.get(reverse("interests:candidates"))
        self.client.force_login(self.user)
        forbidden = self.client.post(
            reverse("interests:review", args=[self.candidate.id]),
            data=json.dumps({"action": "reject"}),
            content_type="application/json",
        )
        self.assertEqual(anonymous.status_code, 302)
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(self.client.get(reverse("interests:candidates")).status_code, 403)

    def test_candidate_date_filter_and_pagination_reach_older_pending_rows(self):
        self.client.force_login(self.staff)
        InterestCandidate.objects.bulk_create([
            InterestCandidate(job=self.job, data_date="2026-08-26", phrase=f"candidate-{index}",
                              normalized_phrase=f"candidate-{index}", query_count=8, users=6, sessions=8)
            for index in range(205)
        ])
        older = self.client.get(reverse("interests:candidates"), {"date": "2026-08-25"}).json()
        self.assertEqual([row["id"] for row in older["items"]], [str(self.candidate.id)])
        first = self.client.get(reverse("interests:candidates"), {"date": "2026-08-26", "limit": 200}).json()
        self.assertEqual((len(first["items"]), first["count"], first["nextOffset"]), (200, 205, 200))
        last = self.client.get(reverse("interests:candidates"), {"date": "2026-08-26", "offset": first["nextOffset"]}).json()
        self.assertEqual(len(last["items"]), 5)
        self.assertIsNone(last["nextOffset"])
        self.assertEqual(self.client.get(reverse("interests:candidates"), {"date": "invalid"}).status_code, 400)

    def test_stale_candidate_instance_cannot_override_an_existing_review(self):
        InterestCandidate.objects.filter(pk=self.candidate.pk).update(status=InterestCandidate.Status.REJECTED)
        with self.assertRaisesMessage(ValueError, "candidate_already_reviewed"):
            review_candidate(self.candidate, self.staff, {"action": "reject"})
        self.assertEqual(self.candidate.review_events.count(), 0)

    def test_approve_creates_entity_alias_audit_and_eight_day_backfill(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("interests:review", args=[self.candidate.id]),
            data=json.dumps({
                "action": "approve",
                "canonicalName": "海底新奇梗",
                "entityType": "网络热梗",
                "entitySubtype": "网络句式",
                "broadTopic": "其他明确内容",
                "aliases": ["野狗奶"],
            }, ensure_ascii=False),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.candidate.refresh_from_db()
        entity = self.candidate.resolved_entity
        self.assertEqual(self.candidate.status, InterestCandidate.Status.APPROVED)
        self.assertEqual(entity.canonical_name, "海底新奇梗")
        self.assertEqual(list(entity.aliases.values_list("value", flat=True)), ["野狗奶"])
        backfill = InterestBackfillRequest.objects.get(entity=entity)
        self.assertEqual(backfill.start_date, date(2026, 8, 25) - timedelta(days=7))
        self.assertEqual(backfill.end_date, date(2026, 8, 25))
        self.assertEqual(self.candidate.review_events.get().details, {"entityRegistryId": entity.registry_id})

    def test_merge_links_existing_entity_and_candidate_payload_is_aggregate_only(self):
        ensure_seed_registry()
        target = InterestEntity.objects.get(registry_id="IE0001")
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("interests:review", args=[self.candidate.id]),
            data=json.dumps({"action": "merge", "targetRegistryId": target.registry_id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, InterestCandidate.Status.MERGED)
        self.assertEqual(self.candidate.resolved_entity_id, target.id)
        self.client.force_login(self.staff)
        listing = self.client.get(reverse("interests:candidates"))
        self.assertTrue(listing.json()["items"][0]["modelRecommended"])
        self.assertEqual(listing.json()["batch"]["dates"], [f"2026-08-{day:02d}" for day in range(19, 26)])
        body = listing.content.decode("utf-8")
        for forbidden in ("examples", "clientId", '"cid"', "ai_text", "context"):
            self.assertNotIn(forbidden, body)

    def test_duplicate_alias_is_rejected(self):
        ensure_seed_registry()
        target = InterestEntity.objects.get(registry_id="IE0002")
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("interests:add-alias", args=[target.registry_id]),
            data=json.dumps({"alias": "奥特曼"}, ensure_ascii=False),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)

    def test_interest_snapshot_endpoint_returns_payload_only(self):
        InterestSnapshot.objects.create(
            data_date=self.job.data_date,
            job=self.job,
            schema_version="teeni-interest-snapshot/1.1.0",
            engine_version="teeni-interest-engine/1.1.0",
            source_sha256="a" * 64,
            registry_sha256="b" * 64,
            workbook_sha256="c" * 64,
            payload={
                "schemaVersion": "teeni-interest-snapshot/1.1.0",
                "dataDate": "2026-08-25",
                "entities": [],
                "restrictedEntities": [{"id": "R1", "name": "风险固定表达", "queries": 3}],
            },
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse("interests:list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["entities"], [])
        self.assertFalse(response.json()["canViewRestricted"])
        self.assertNotIn("restrictedEntities", response.json()["items"][0])
        self.assertNotIn("publishedAt", response.json()["items"][0])

        self.client.force_login(self.staff)
        staff_response = self.client.get(reverse("interests:list"))
        self.assertTrue(staff_response.json()["canViewRestricted"])
        self.assertEqual(staff_response.json()["items"][0]["restrictedEntities"][0]["queries"], 3)

    def test_segment_summary_and_groups_restore_sparse_metrics(self):
        InterestSegmentSnapshot.objects.create(
            data_date=self.job.data_date,
            job=self.job,
            dimension_set="gender",
            schema_version="teeni-interest-segments/1.0.0",
            source_sha256="a" * 64,
            registry_sha256="b" * 64,
            province_map_version="teeni-city-province-map/1.0.0",
            province_map_sha256="c" * 64,
            payload={
                "schemaVersion": "teeni-interest-segments/1.0.0",
                "dataDate": "2026-08-25",
                "dimensionSet": "gender",
                "dimensions": ["gender"],
                "sampleMinimumUsers": 30,
                "domains": {
                    "ages": list(range(1, 18)),
                    "genders": [{"id": "male", "label": "男"}, {"id": "female", "label": "女"}],
                    "regions": [],
                },
                "overallEligibleUsers": 10,
                "overallIps": [{"ipId": "IP1", "interestUsers": 3, "overallCoverage": 0.3}],
                "groups": [
                    {
                        "groupKey": "male", "values": {"gender": "male"}, "groupUsers": 60,
                        "eligibleUsers": 5, "sampleStatus": "可描述",
                        "ipCounts": [{"ipId": "IP1", "interestUsers": 2}],
                    },
                    {
                        "groupKey": "female", "values": {"gender": "female"}, "groupUsers": 5,
                        "eligibleUsers": 5, "sampleStatus": "样本不足", "ipCounts": [],
                    },
                ],
            },
        )
        self.client.force_login(self.user)
        summary = self.client.get(reverse("interests:segments"), {"date": "2026-08-25", "dimensionSet": "gender"})
        self.assertEqual(summary.status_code, 200)
        self.assertTrue(summary.json()["available"])
        self.assertEqual(summary.json()["groups"][0]["sampleStatus"], "样本不足")
        self.assertNotIn("ipCounts", summary.content.decode("utf-8"))
        groups = self.client.get(
            reverse("interests:segment-groups"),
            {"date": "2026-08-25", "dimensionSet": "gender", "groups": "male,female"},
        )
        self.assertEqual(groups.status_code, 200)
        male, female = groups.json()["groups"]
        self.assertEqual(male["sampleStatus"], "样本不足")
        self.assertEqual(male["ips"][0]["interestUsers"], 2)
        self.assertAlmostEqual(male["ips"][0]["groupCoverage"], 0.4)
        self.assertAlmostEqual(male["ips"][0]["percentagePointDifference"], 0.1)
        self.assertEqual(female["ips"][0]["interestUsers"], 0)
        for forbidden in ("clientId", '"cid"', "source_row", "profile_age", "city_normalized"):
            self.assertNotIn(forbidden, groups.content.decode("utf-8"))

    def test_segment_query_rejects_invalid_or_duplicate_groups(self):
        self.client.force_login(self.user)
        endpoint = reverse("interests:segment-groups")
        for query in (
            {"date": "bad", "dimensionSet": "gender", "groups": "male"},
            {"date": "2026-08-25", "dimensionSet": "bad", "groups": "male"},
            {"date": "2026-08-25", "dimensionSet": "gender", "groups": "male,male"},
            {"date": "2026-08-25", "dimensionSet": "gender", "groups": "a,b,c,d,e"},
        ):
            self.assertEqual(self.client.get(endpoint, query).status_code, 400)

    def test_legacy_1_3_age_gender_remains_available_only_for_that_dimension(self):
        demographic = {
            "overallEligibleUsers": 1,
            "overallIps": [{"ipId": "IP1", "interestUsers": 1, "overallCoverage": 1.0}],
            "groups": [
                {
                    "age": age,
                    "gender": gender,
                    "groupUsers": 1 if (age, gender) == (6, "男") else 0,
                    "eligibleUsers": 1 if (age, gender) == (6, "男") else 0,
                    "sampleStatus": "样本不足",
                    "ips": [{
                        "ipId": "IP1",
                        "interestUsers": 1 if (age, gender) == (6, "男") else 0,
                        "groupCoverage": 1.0 if (age, gender) == (6, "男") else None,
                        "percentagePointDifference": 0.0 if (age, gender) == (6, "男") else None,
                    }],
                }
                for age in range(1, 18)
                for gender in ("男", "女")
            ],
        }
        InterestSnapshot.objects.create(
            data_date=self.job.data_date,
            job=self.job,
            schema_version="teeni-interest-snapshot/1.3.0",
            engine_version="teeni-interest-engine/1.3.0",
            source_sha256="a" * 64,
            registry_sha256="b" * 64,
            workbook_sha256="c" * 64,
            payload={"schemaVersion": "teeni-interest-snapshot/1.3.0", "demographicInterest": demographic},
        )
        self.client.force_login(self.user)
        supported = self.client.get(reverse("interests:segments"), {"date": "2026-08-25", "dimensionSet": "age_gender"})
        unavailable = self.client.get(reverse("interests:segments"), {"date": "2026-08-25", "dimensionSet": "region"})
        self.assertTrue(supported.json()["available"])
        self.assertTrue(supported.json()["legacy"])
        self.assertFalse(unavailable.json()["available"])

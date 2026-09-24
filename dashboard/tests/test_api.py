import json
from unittest.mock import patch

from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from dashboard.models import DailySnapshot, ManagementInsight


CONSTELLATIONS = [
    "摩羯座", "水瓶座", "双鱼座", "白羊座", "金牛座", "双子座",
    "巨蟹座", "狮子座", "处女座", "天秤座", "天蝎座", "射手座",
]


def sample_payload(data_date: str = "2026-08-17") -> dict:
    return {
        "schemaVersion": "teeni-dashboard-snapshot/1.0.0",
        "dataDate": data_date,
        "generatedAt": "2026-08-18T00:00:00+00:00",
        "sceneId": "488",
        "coreVersion": "2.4.0",
        "rulesVersion": "10.0.0",
        "contractVersion": "teeni-base-detail/1.1.0",
        "sourceSha256": "a" * 64,
        "workbookSha256": "b" * 64,
        "metrics": {
            "volume": {"turns": 100, "sessions": 10, "users": 5},
            "participation": {"sessions": 8, "eligibleSessions": 10, "rate": 0.8},
            "opening": {"exposures": 100, "opened": 48, "rate": 0.48},
            "depth": {
                "rawAverage": 10.0,
                "postTemplateAverage": 12.0,
                "netValidAverage": 13.0,
            },
            "riskSummary": {},
            "reopen": {},
            "coverage": {},
            "endings": {},
            "qualityCategories": [],
            "aiSafetyCategories": [],
            "userRiskCategories": [],
            "intents": {},
            "demographics": [],
        },
    }


def current_payload(
    data_date: str = "2026-09-01",
    product_version: str = "M2",
    scene_id: str = "904",
) -> dict:
    payload = sample_payload(data_date)
    payload.update(
        {
            "schemaVersion": "teeni-dashboard-snapshot/1.2.0",
            "productVersion": product_version,
            "sceneId": scene_id,
            "coreVersion": "2.5.0",
            "rulesVersion": "12.0.0",
            "contractVersion": "teeni-base-detail/1.2.0",
        }
    )
    payload["metrics"]["depth"].update(
        {
            "rawMedian": 4.0,
            "postTemplateMedian": 5.0,
            "netValidMedian": 6.0,
            "rawFivePlusShare": 0.3,
            "postTemplateFivePlusShare": 0.4,
            "netValidFivePlusShare": 0.5,
            "netValidSessions": 8,
        }
    )
    payload["metrics"]["coverage"] = {
        "intentParsed": 80,
        "intentMissing": 20,
        "intentRate": 0.8,
        "profileUsers": 0,
        "profileEligibleUsers": 0,
        "profileRate": 0.0,
        "locationUsers": 0,
        "locationEligibleUsers": 0,
        "locationRate": 0.0,
        "constellationUsers": 0,
        "constellationEligibleUsers": 0,
        "constellationRate": 0.0,
    }
    payload["metrics"]["locations"] = {
        "eligibleUsers": 0,
        "normalUsers": 0,
        "coverageRate": 0.0,
        "statuses": [],
        "items": [],
    }
    payload["metrics"]["constellations"] = {
        "eligibleUsers": 0,
        "normalUsers": 0,
        "coverageRate": 0.0,
        "statuses": [],
        "items": [
            {
                "label": label,
                "users": 0,
                "userShare": 0.0,
                "sessions": 0,
                "netValidTurns": 0,
                "averageDepth": 0.0,
                "fivePlusSessions": 0,
                "fivePlusShare": 0.0,
                "sampleStatus": "样本不足",
            }
            for label in CONSTELLATIONS
        ],
    }
    return payload


@override_settings(
    TEENI_PUBLISH_TOKEN="test-publish-token",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    },
)
class DashboardApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="manager@example.com",
            email="manager@example.com",
            password="test-password-42",
        )

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse("dashboard:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_active_email_account_can_read_aggregate_data(self):
        self.client.login(username="manager@example.com", password="test-password-42")
        DailySnapshot.objects.create(
            data_date="2026-08-17",
            scene_id="488",
            core_version="2.4.0",
            rules_version="10.0.0",
            contract_version="teeni-base-detail/1.1.0",
            source_sha256="a" * 64,
            workbook_sha256="b" * 64,
            payload=sample_payload(),
        )
        response = self.client.get(reverse("dashboard:data"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schemaVersion"], "teeni-dashboard-response/1.2.0")
        self.assertEqual(response.json()["selectedProductVersion"], "M1")
        self.assertEqual(response.json()["latestDate"], "2026-08-17")
        self.assertEqual(response.json()["snapshots"][0]["productVersion"], "M1")
        self.assertEqual(response.json()["versions"][0]["latestDate"], "2026-08-17")
        self.assertNotIn("locations", response.json()["snapshots"][0]["metrics"])
        self.assertNotIn("manager@example.com", response.content.decode("utf-8"))

    def test_inactive_account_cannot_login(self):
        self.user.is_active = False
        self.user.save()
        logged_in = self.client.login(username="manager@example.com", password="test-password-42")
        self.assertFalse(logged_in)

    def test_password_change_uses_teeni_template(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("password_change"))
        self.assertContains(response, "修改密码")
        self.assertContains(response, "保存新密码")
        self.assertTemplateUsed(response, "dashboard/password_change_form.html")

    def test_password_change_done_uses_teeni_template(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("password_change_done"))
        self.assertContains(response, "密码已更新")
        self.assertTemplateUsed(response, "dashboard/password_change_done.html")

    def test_registration_normalizes_email_and_logs_in(self):
        response = self.client.post(
            reverse("register"),
            data={
                "email": "  NEW.Member@Example.COM ",
                "password1": "registration-password-42",
                "password2": "registration-password-42",
            },
        )
        self.assertRedirects(response, reverse("dashboard:index"))
        user = get_user_model().objects.get(username="new.member@example.com")
        self.assertEqual(user.email, "new.member@example.com")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.id)
        self.assertEqual(self.client.get(reverse("topics:list-jobs")).status_code, 200)

    def test_registration_rejects_case_insensitive_duplicate_email(self):
        response = self.client.post(
            reverse("register"),
            data={
                "email": "MANAGER@EXAMPLE.COM",
                "password1": "registration-password-42",
                "password2": "registration-password-42",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "该邮箱已注册")
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_registration_rejects_password_mismatch_and_short_password(self):
        mismatch = self.client.post(
            reverse("register"),
            data={
                "email": "mismatch@example.com",
                "password1": "registration-password-42",
                "password2": "registration-password-43",
            },
        )
        short = self.client.post(
            reverse("register"),
            data={
                "email": "short@example.com",
                "password1": "abcdefghijklm",
                "password2": "abcdefghijklm",
            },
        )
        self.assertContains(mismatch, "两次输入的密码不一致")
        self.assertContains(short, "密码长度至少为 14 个字符")
        self.assertFalse(get_user_model().objects.filter(email__in=["mismatch@example.com", "short@example.com"]).exists())

    def test_registration_uses_only_safe_next_redirects(self):
        safe = self.client.post(
            f'{reverse("register")}?next={reverse("password_change")}',
            data={
                "email": "safe-next@example.com",
                "password1": "registration-password-42",
                "password2": "registration-password-42",
                "next": reverse("password_change"),
            },
        )
        self.assertRedirects(safe, reverse("password_change"), fetch_redirect_response=False)
        self.client.logout()
        unsafe = self.client.post(
            reverse("register"),
            data={
                "email": "unsafe-next@example.com",
                "password1": "registration-password-42",
                "password2": "registration-password-42",
                "next": "https://example.net/steal-session",
            },
        )
        self.assertRedirects(unsafe, reverse("dashboard:index"))

    def test_registration_page_has_two_password_toggles(self):
        response = self.client.get(reverse("register"))
        self.assertContains(response, "创建账号")
        self.assertContains(response, 'data-password-toggle', count=2)

    def test_publish_rejects_wrong_token(self):
        response = self.client.post(
            reverse("dashboard:publish"),
            data=json.dumps(sample_payload()),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer wrong-token",
        )
        self.assertEqual(response.status_code, 401)

    def test_publish_upserts_by_date(self):
        url = reverse("dashboard:publish")
        headers = {"HTTP_AUTHORIZATION": "Bearer test-publish-token"}
        first = self.client.post(url, data=json.dumps(sample_payload()), content_type="application/json", **headers)
        payload = sample_payload()
        payload["metrics"]["volume"] = {"turns": 123}
        second = self.client.post(url, data=json.dumps(payload), content_type="application/json", **headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["dataDate"], payload["dataDate"])
        self.assertEqual(second.json()["productVersion"], "M1")
        self.assertEqual(second.json()["sceneId"], "488")
        self.assertEqual(second.json()["workbookSha256"], payload["workbookSha256"])
        self.assertEqual(DailySnapshot.objects.count(), 1)
        self.assertEqual(DailySnapshot.objects.get().payload["metrics"]["volume"]["turns"], 123)
        self.assertEqual(ManagementInsight.objects.filter(origin="automatic").count(), 0)

    def test_same_date_m1_and_m2_coexist_and_upsert_independently(self):
        url = reverse("dashboard:publish")
        headers = {"HTTP_AUTHORIZATION": "Bearer test-publish-token"}
        m1 = sample_payload("2026-09-01")
        m2 = current_payload("2026-09-01")

        first = self.client.post(url, data=json.dumps(m1), content_type="application/json", **headers)
        second = self.client.post(url, data=json.dumps(m2), content_type="application/json", **headers)
        m2["metrics"]["volume"]["turns"] = 222
        replacement = self.client.post(url, data=json.dumps(m2), content_type="application/json", **headers)

        self.assertEqual((first.status_code, second.status_code, replacement.status_code), (201, 201, 201))
        self.assertEqual(DailySnapshot.objects.count(), 2)
        self.assertEqual(
            DailySnapshot.objects.get(product_version="M2").payload["metrics"]["volume"]["turns"],
            222,
        )
        self.assertEqual(DailySnapshot.objects.get(product_version="M1").scene_id, "488")
        self.assertEqual(ManagementInsight.objects.filter(origin="automatic").count(), 0)

    def test_dashboard_filters_versions_and_m2_has_no_m1_baseline(self):
        self.client.force_login(self.user)
        payload = current_payload()
        DailySnapshot.objects.create(
            data_date=payload["dataDate"],
            product_version="M2",
            scene_id="904",
            core_version=payload["coreVersion"],
            rules_version=payload["rulesVersion"],
            contract_version=payload["contractVersion"],
            source_sha256=payload["sourceSha256"],
            workbook_sha256=payload["workbookSha256"],
            payload=payload,
        )

        response = self.client.get(reverse("dashboard:data"), {"productVersion": "M2"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["selectedProductVersion"], "M2")
        self.assertEqual(response.json()["latestDate"], "2026-09-01")
        self.assertIsNone(response.json()["baseline"])
        self.assertEqual({item["productVersion"] for item in response.json()["snapshots"]}, {"M2"})

    def test_dashboard_rejects_unknown_product_version(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:data"), {"productVersion": "M3"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_product_version")

    def test_database_rejects_product_scene_mismatch(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            DailySnapshot.objects.create(
                data_date="2026-09-01",
                product_version="M2",
                scene_id="488",
                core_version="2.5.0",
                rules_version="12.0.0",
                contract_version="teeni-base-detail/1.2.0",
                source_sha256="a" * 64,
                workbook_sha256="b" * 64,
                payload=current_payload(),
            )

    def test_publish_rejects_sensitive_payload(self):
        payload = sample_payload()
        payload["metrics"]["clientId"] = "sensitive"
        response = self.client.post(
            reverse("dashboard:publish"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer test-publish-token",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(DailySnapshot.objects.count(), 0)


class EnsureAdminTests(TestCase):
    def test_rerun_does_not_reset_changed_password(self):
        environment = {
            "TEENI_ADMIN_EMAIL": "admin@example.com",
            "TEENI_ADMIN_PASSWORD": "initial-password-42",
        }
        with patch.dict("os.environ", environment):
            call_command("ensure_admin")

        user = get_user_model().objects.get(username="admin@example.com")
        user.set_password("changed-password-84")
        user.save()

        with patch.dict("os.environ", environment):
            call_command("ensure_admin")

        self.assertIsNotNone(
            authenticate(username="admin@example.com", password="changed-password-84")
        )
        self.assertIsNone(
            authenticate(username="admin@example.com", password="initial-password-42")
        )

    def test_reset_password_requires_explicit_flag(self):
        environment = {
            "TEENI_ADMIN_EMAIL": "admin@example.com",
            "TEENI_ADMIN_PASSWORD": "initial-password-42",
        }
        with patch.dict("os.environ", environment):
            call_command("ensure_admin")
        reset_environment = {**environment, "TEENI_ADMIN_PASSWORD": "reset-password-84"}
        with patch.dict("os.environ", reset_environment):
            call_command("ensure_admin", reset_password=True)

        self.assertIsNotNone(
            authenticate(username="admin@example.com", password="reset-password-84")
        )
        self.assertIsNone(
            authenticate(username="admin@example.com", password="initial-password-42")
        )

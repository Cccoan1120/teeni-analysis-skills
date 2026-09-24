import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from interests.models import InterestAlias, InterestEntity, InterestReviewEvent
from interests.registry import ensure_seed_registry, freeze_registry, registry_payload
from topics.models import TopicJob


class InterestRegistryTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = override_settings(TEENI_TOPIC_STORAGE_ROOT=Path(self.temp.name))
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        self.user = get_user_model().objects.create_user("registry@example.com", password="a-strong-password-42")

    def job(self):
        return TopicJob.objects.create(
            created_by=self.user,
            data_date="2026-08-25",
            original_name="scene488.csv",
            expected_size=1,
        )

    def test_seed_registry_and_frozen_snapshot_are_deterministic(self):
        ensure_seed_registry()
        payload = registry_payload()
        self.assertEqual(len(payload["entities"]), 132)
        paw_patrol = next(item for item in payload["entities"] if item["canonicalName"] == "汪汪队立大功")
        self.assertIn(
            {"value": "汪汪队", "mode": "substring", "policy": "auto"},
            paw_patrol["matchRules"],
        )
        job = self.job()
        first = freeze_registry(job)
        second = freeze_registry(job)
        self.assertEqual(first.id, second.id)
        self.assertRegex(first.sha256, r"^[0-9a-f]{64}$")
        meme = next(item for item in payload["entities"] if item["canonicalName"] == "野生狗奶")
        self.assertEqual(meme["entitySubtype"], "抽象食品")
        self.assertEqual(meme["visibility"], "public")
        risk = next(item for item in payload["entities"] if item["canonicalName"] == "唐")
        self.assertEqual(risk["visibility"], "restricted")
        self.assertEqual(risk["matchRules"][0]["policy"], "candidate")
        expected_parents = {
            "赛罗": "奥特曼",
            "艾莎公主": "冰雪奇缘",
            "孙悟空": "西游记",
            "蜘蛛侠": "漫威超级英雄",
        }
        names_by_id = {item["id"]: item["canonicalName"] for item in payload["entities"]}
        for child_name, parent_name in expected_parents.items():
            child = next(item for item in payload["entities"] if item["canonicalName"] == child_name)
            self.assertEqual(names_by_id[child["parentRegistryId"]], parent_name)
            self.assertEqual(child["entitySubtype"], "作品角色")

    def test_alias_cannot_shadow_another_canonical_name(self):
        first = InterestEntity.objects.create(
            canonical_name="奥特曼",
            entity_type="作品",
            broad_topic="影视动漫与角色",
        )
        second = InterestEntity.objects.create(
            canonical_name="汪汪队立大功",
            entity_type="作品",
            broad_topic="影视动漫与角色",
        )
        alias = InterestAlias(entity=second, value=first.canonical_name)
        with self.assertRaises(ValidationError):
            alias.full_clean()

    def test_review_event_cannot_be_changed_or_deleted(self):
        from interests.models import InterestCandidate

        job = self.job()
        candidate = InterestCandidate.objects.create(
            job=job,
            data_date=job.data_date,
            phrase="野生狗奶",
            normalized_phrase="野生狗奶",
            query_count=8,
            users=5,
            sessions=8,
        )
        event = InterestReviewEvent.objects.create(candidate=candidate, actor=self.user, action="reject")
        event.action = "approve"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()

    def test_parent_hierarchy_rejects_cycles_and_fourth_level(self):
        first = InterestEntity.objects.create(canonical_name="第一层", entity_type="作品", broad_topic="影视动漫与角色")
        second = InterestEntity.objects.create(canonical_name="第二层", entity_type="角色", broad_topic="影视动漫与角色", parent=first)
        third = InterestEntity.objects.create(canonical_name="第三层", entity_type="角色", broad_topic="影视动漫与角色", parent=second)
        fourth = InterestEntity(canonical_name="第四层", entity_type="角色", broad_topic="影视动漫与角色", parent=third)
        with self.assertRaisesRegex(ValidationError, "three levels"):
            fourth.full_clean()
        first.parent = third
        with self.assertRaisesRegex(ValidationError, "acyclic"):
            first.full_clean()

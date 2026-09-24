import unittest
from pathlib import Path

from interest_engine.candidates import candidate_phrases
from interest_engine.classifier import (
    Behavior,
    InterestSignal,
    QueryRoute,
    Turn,
    TurnContext,
    classify_turn,
)
from interest_engine.registry import BroadTopic, EntityRegistry, Visibility


def registry():
    return EntityRegistry.from_dict({
        "schemaVersion": "teeni-interest-registry/1.0.0",
        "registryVersion": "test",
        "entities": [
            {
                "id": "IE0001",
                "canonicalName": "奥特曼",
                "entityType": "作品",
                "broadTopic": "影视动漫与角色",
                "aliases": ["Ultraman"],
                "ambiguousAliases": ["小爱"],
            },
            {
                "id": "IE0002",
                "canonicalName": "汪汪队立大功",
                "entityType": "作品",
                "broadTopic": "影视动漫与角色",
                "aliases": ["汪汪队"],
            },
            {
                "id": "IE0003",
                "canonicalName": "植物大战僵尸",
                "entityType": "游戏",
                "broadTopic": "电子游戏与互动玩法",
                "aliases": [],
            },
        ],
    })


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.registry = registry()

    def test_entity_matching_precedes_short_noise_filter(self):
        result = classify_turn(Turn("奥特曼"), TurnContext(), self.registry)
        self.assertEqual(result.route, QueryRoute.VALID_CONTENT)
        self.assertEqual(result.entity_matches[0].entity.canonical_name, "奥特曼")
        self.assertEqual(result.interest_signal, InterestSignal.INITIATION)

    def test_long_alias_wins_and_maps_to_one_canonical_entity(self):
        result = classify_turn(Turn("给我讲汪汪队立大功故事"), TurnContext(), self.registry)
        self.assertEqual([item.entity.id for item in result.entity_matches], ["IE0002"])
        self.assertEqual(result.behavior, Behavior.CONTENT_REQUEST)

    def test_ambiguous_alias_is_not_automatically_linked(self):
        result = classify_turn(Turn("小爱是谁"), TurnContext(), self.registry)
        self.assertEqual(result.entity_matches, ())

    def test_invalid_template_never_enters_content_analysis(self):
        result = classify_turn(Turn("奥特曼", is_invalid_turn="是"), TurnContext(), self.registry)
        self.assertEqual(result.route, QueryRoute.INVALID_TEMPLATE)
        self.assertEqual(result.entity_matches, ())

    def test_ai_prompted_alias_only_reply_is_passive(self):
        result = classify_turn(
            Turn("奥特曼"),
            TurnContext(previous_ai_text="你喜欢奥特曼吗"),
            self.registry,
        )
        self.assertEqual(result.interest_signal, InterestSignal.PASSIVE)

    def test_substantive_follow_up_is_active_continuation(self):
        result = classify_turn(
            Turn("奥特曼为什么这么厉害"),
            TurnContext(previous_ai_text="我们来聊奥特曼吧"),
            self.registry,
        )
        self.assertEqual(result.interest_signal, InterestSignal.CONTINUATION)

    def test_non_content_routes_remain_auditable(self):
        cases = {
            "嗯": QueryRoute.PURE_DIALOGUE,
            "我也不知道呀": QueryRoute.PURE_DIALOGUE,
            "你叫什么名字": QueryRoute.PURE_DIALOGUE,
            "调大音量": QueryRoute.DEVICE_CONTROL,
            "小声点": QueryRoute.DEVICE_CONTROL,
            "啊啊啊啊": QueryRoute.ASR_NOISE,
            "继续": QueryRoute.INSUFFICIENT_CONTEXT,
            "继续讲": QueryRoute.INSUFFICIENT_CONTEXT,
            "妈妈": QueryRoute.INSUFFICIENT_CONTEXT,
            "九十九": QueryRoute.INSUFFICIENT_CONTEXT,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify_turn(Turn(text), TurnContext(), self.registry).route, expected)

    def test_unknown_and_ordinary_negation_are_not_sharing_or_correction(self):
        for text in ("今天是星期三", "奥特曼不是坏人", "不是每个奥特曼都是坏人"):
            result = classify_turn(Turn(text), TurnContext(previous_entity_ids=("IE0002",)), self.registry)
            self.assertEqual(result.behavior, Behavior.UNKNOWN)
        result = classify_turn(Turn("你听错了，我说的是奥特曼"), TurnContext(), self.registry)
        self.assertEqual(result.behavior, Behavior.CORRECTION)

    def test_entity_signals_are_independent_in_one_query(self):
        result = classify_turn(Turn("奥特曼，讲讲汪汪队的故事"),
                               TurnContext(previous_ai_entity_ids=("IE0001",)), self.registry)
        signals = {match.entity.id: signal for match, signal in zip(result.entity_matches, result.entity_signals)}
        self.assertEqual(signals, {"IE0001": InterestSignal.PASSIVE, "IE0002": InterestSignal.INITIATION})
        self.assertEqual(result.interest_signal, InterestSignal.NONE)

    def test_conservative_context_inheritance(self):
        result = classify_turn(Turn("继续讲"), TurnContext(previous_entity_ids=("IE0001",)), self.registry)
        self.assertEqual(result.route, QueryRoute.VALID_CONTENT)
        self.assertEqual(result.entity_signals, (InterestSignal.CONTINUATION,))
        self.assertEqual(result.match_bases, ("single_context",))
        ambiguous = classify_turn(Turn("然后呢"), TurnContext(previous_entity_ids=("IE0001",), previous_ai_entity_ids=("IE0002",)), self.registry)
        self.assertEqual(ambiguous.route, QueryRoute.INSUFFICIENT_CONTEXT)

    def test_positive_negative_and_neutral_preferences(self):
        result = classify_turn(Turn("我喜欢奥特曼，但我讨厌汪汪队"), TurnContext(), self.registry)
        polarities = {match.entity.id: value for match, value in zip(result.entity_matches, result.preference_polarities)}
        self.assertEqual(polarities, {"IE0001": "positive", "IE0002": "negative"})

        self.assertEqual(classify_turn(Turn("奥特曼"), TurnContext(), self.registry).preference_polarities, ("neutral",))
        result = classify_turn(Turn("我喜欢奥特曼讨厌汪汪队"), TurnContext(), self.registry)
        polarities = {match.entity.id: value for match, value in zip(result.entity_matches, result.preference_polarities)}
        self.assertEqual(polarities, {"IE0001": "positive", "IE0002": "negative"})

    def test_known_entity_does_not_hide_cooccurring_candidate(self):
        self.assertEqual(candidate_phrases("奥特曼和神秘新作品", ("奥特曼",)), ("神秘新作品",))

    def test_broad_topics_are_separate_from_behavior(self):
        cases = {
            "给我讲一个童话故事": BroadTopic.STORIES,
            "我们玩猜数字游戏": BroadTopic.VIDEO_GAMES,
            "数学加法怎么做": BroadTopic.LEARNING,
            "妈妈今天来学校": BroadTopic.RELATIONSHIPS,
            "我的小狗生病了": BroadTopic.ANIMALS,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                result = classify_turn(Turn(text), TurnContext(), self.registry)
                self.assertEqual(result.route, QueryRoute.VALID_CONTENT)
                self.assertEqual(result.broad_topic, expected)

    def test_candidate_extraction_suppresses_dialogue_but_keeps_trackable_phrases(self):
        for text in (
            "我不知道", "妈妈", "可以呀", "哈哈哈", "你说啥", "你好呀", "知道了", "现在几点了",
            "吃什么", "开心", "这个", "我喜欢", "睡觉", "喜欢呀", "好的好的", "你再说一遍",
            "一加一等于几", "玩游戏", "不会的", "游戏", "没有没有",
            "嗯。不知道。", "吃的", "草莓味", "故事", "一下", "Yes", "红色的", "小朋友", "玩玩具",
            "看电视", "巧克力", "apple", "第一个", "对不起",
        ):
            with self.subTest(text=text):
                self.assertEqual(candidate_phrases(text), ())
        self.assertEqual(candidate_phrases("野生狗奶"), ("野生狗奶",))
        self.assertEqual(candidate_phrases("数字炸弹"), ("数字炸弹",))
        self.assertEqual(candidate_phrases("海龟汤"), ("海龟汤",))
        self.assertEqual(candidate_phrases("孙悟空"), ("孙悟空",))
        self.assertEqual(candidate_phrases("石头剪刀布"), ("石头剪刀布",))

    def test_seed_registry_contains_original_and_word_curated_entities(self):
        seed = Path(__file__).resolve().parents[1] / "resources" / "entity-registry-v1.json"
        loaded = EntityRegistry.from_json(seed)
        self.assertEqual(len(loaded.entities), 132)
        self.assertEqual(len({item.id for item in loaded.entities}), 132)
        self.assertEqual(len({item.canonical_name for item in loaded.entities}), 132)
        self.assertEqual(loaded.match("我们聊汪汪队吧")[0].entity.canonical_name, "汪汪队立大功")
        self.assertEqual(loaded.match("先玩植物大战僵尸")[0].entity.entity_type.value, "游戏")

        for ordinary_text in (
            "祝你有福", "背一首唐诗", "把杯子拿来", "江苏南通天气", "Teddy bear",
            "你干嘛呢", "拉拉链",
        ):
            with self.subTest(ordinary_text=ordinary_text):
                self.assertEqual(loaded.match(ordinary_text), ())

        for phrase in ("野生狗奶", "中国人能飞", "牛来"):
            with self.subTest(phrase=phrase):
                self.assertEqual(loaded.match(phrase)[0].entity.canonical_name, phrase)

        restricted = loaded.match("小明是gay")
        self.assertEqual(len(restricted), 1)
        self.assertEqual(restricted[0].entity.visibility, Visibility.RESTRICTED)
        self.assertEqual(loaded.match("老师说小明是gay但这句话后面还有内容"), ())

        collection = loaded.get("IE0077")
        self.assertEqual(collection.canonical_name, "6 个民间无限宝石")
        self.assertEqual(
            {match.entity.id for match in loaded.match("薛定谔的煎饼和春秋肠")},
            {"IE0077"},
        )

    def test_registry_rejects_cycles_and_unbounded_patterns(self):
        payload = {
            "schemaVersion": "teeni-interest-registry/1.1.0",
            "registryVersion": "bad",
            "entities": [
                {
                    "id": "A", "canonicalName": "甲", "entityType": "其他",
                    "broadTopic": "其他明确内容", "parentRegistryId": "B",
                    "matchRules": [{"value": "甲", "mode": "exact", "policy": "auto"}],
                },
                {
                    "id": "B", "canonicalName": "乙", "entityType": "其他",
                    "broadTopic": "其他明确内容", "parentRegistryId": "A",
                    "matchRules": [{"value": "乙", "mode": "exact", "policy": "auto"}],
                },
            ],
        }
        with self.assertRaisesRegex(ValueError, "acyclic"):
            EntityRegistry.from_dict(payload)

        payload["entities"] = [{
            "id": "A", "canonicalName": "句式", "entityType": "网络热梗",
            "broadTopic": "其他明确内容",
            "matchRules": [{"value": ".*是gay.*", "mode": "pattern", "policy": "auto"}],
        }]
        with self.assertRaisesRegex(ValueError, "anchored"):
            EntityRegistry.from_dict(payload)


if __name__ == "__main__":
    unittest.main()

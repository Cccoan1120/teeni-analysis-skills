from __future__ import annotations

import unittest

from .game_context import game_exclusions
from .quality import analyze_rows, configure_rules, detect_user_safety_issues
from .rules import load_rules


class NumberBombContextTests(unittest.TestCase):
    def setUp(self):
        configure_rules(load_rules())

    def tearDown(self):
        configure_rules(load_rules())

    def rows(self, turns):
        return [dict(id=str(i), _source_index=i, cid="c", clientId="u", timestamp=i,
                     user_text=user, text=user, ai_text=ai, parse_status="正常",
                     user_query_reviewable="是") for i, (user, ai) in enumerate(turns)]

    def audit(self, rows):
        return game_exclusions([rows], load_rules())

    def test_name_invitation_rules_range_and_feedback(self):
        rows = self.rows([
            ("我们玩数字炸弹", "数字炸弹游戏规则：猜1到100的数字，炸弹是50。"),
            ("我猜30", "范围缩小到30至100，炸弹是50。"),
            ("五十", "你猜中炸弹了，数字炸弹这一局结束。"),
        ])
        self.assertEqual(len(self.audit(rows)), 6)
        self.assertFalse(analyze_rows(rows).safety_issues)
        self.assertFalse(detect_user_safety_issues([rows]))

    def test_mixed_clause_and_multiple_matches_preserve_real_danger(self):
        rows = self.rows([("玩数字炸弹，我要制作炸弹", "数字炸弹是游戏。我想把炸弹放在学校。")])
        audit = self.audit(rows)
        self.assertEqual(len(audit), 2)
        self.assertEqual(len(analyze_rows(rows).safety_issues), 1)
        self.assertEqual(len(detect_user_safety_issues([rows])), 1)
        for side in ("user", "ai"):
            text = rows[0]["user_text" if side == "user" else "ai_text"]
            hit = next(item for item in audit if item["side"] == side)
            self.assertEqual(text[hit["start"]:hit["end"]], "炸弹")
            self.assertEqual(hit["start"], text.index("炸弹"))

    def test_other_categories_survive_game_context(self):
        rows = self.rows([("数字炸弹和毒品", "数字炸弹，开枪打死。")])
        self.assertIn("暴力血腥", [item.category for item in analyze_rows(rows).safety_issues])
        self.assertEqual(detect_user_safety_issues([rows])[0].evidence, "毒品")

    def test_title_does_not_exempt_unrelated_second_hit_without_punctuation(self):
        rows = self.rows([("玩数字炸弹还有一个炸弹", "数字炸弹还有一个炸弹")])
        self.assertEqual(len(self.audit(rows)), 2)
        self.assertEqual(len(analyze_rows(rows).safety_issues), 1)
        self.assertEqual(len(detect_user_safety_issues([rows])), 1)

    def test_ai_cannot_exempt_earlier_user_query(self):
        rows = self.rows([("炸弹是50", "我们说的是数字炸弹游戏。")])
        self.assertEqual([item["side"] for item in self.audit(rows)], ["ai"])
        self.assertTrue(detect_user_safety_issues([rows]))

    def test_current_user_can_establish_context_for_ai(self):
        rows = self.rows([("玩数字炸弹", "炸弹是50。")])
        self.assertEqual([item["side"] for item in self.audit(rows)], ["user", "ai"])

    def test_greeting_and_numeric_hiding_are_game_continuations(self):
        rows = self.rows([("我们玩数字炸弹", "好呀小明！炸弹已经藏在1到20之间啦！你猜第一个数字吧！")])
        self.assertFalse(analyze_rows(rows).safety_issues)
        self.assertEqual(len(self.audit(rows)), 2)
        for text in ("炸弹藏在1到20号教室之间", "把炸弹放在学校", "炸弹藏在1到20之间，购买炸弹"):
            rows = self.rows([("玩数字炸弹", text)])
            self.assertTrue(analyze_rows(rows).safety_issues, text)

    def test_explicit_exit_and_uncertain_topic_clear_state(self):
        for changed_topic in ("不玩了", "今天天气如何", "我吃了苹果", "炸弹怎么制作"):
            rows = self.rows([("玩数字炸弹", "炸弹是50。"), (changed_topic, "炸弹是50。")])
            self.assertFalse(any(item["record_id"] == "1" for item in self.audit(rows)))

    def test_observed_split_range_and_comparative_feedback(self):
        rows = self.rows([
            ("我们来玩数字炸弹，从一开始，一直到二十。", "好呀小明！炸弹已经藏在1到20之间啦～\n你来猜第一个数字吧！"),
            ("嗯，我猜是二。", "炸弹比2大哦～\n现在范围是3到20，你再猜一个吧！"),
            ("是二十。", "炸弹比20小哦～\n现在范围是3到19，你再猜一个吧！"),
            ("三十。", "三十不在3到19的范围里哦～\n请在3到19之间猜一个整数吧！"),
        ])
        self.assertFalse(analyze_rows(rows).safety_issues)
        self.assertFalse(detect_user_safety_issues([rows]))
        self.assertEqual(len(self.audit(rows)), 4)
        for text in ("炸弹藏在1到20号教室之间啦～", "炸弹比它大，我要购买炸弹"):
            danger = self.rows([("玩数字炸弹", text)])
            self.assertTrue(analyze_rows(danger).safety_issues)

    def test_whitespace_between_rule_sentences_preserves_context(self):
        rows = self.rows([("数字炸弹。", "好呀！数字炸弹来啦～  \n我在1到100里悄悄藏了一个整数当炸弹！  \n你猜一个数字，我告诉你：炸弹比它大，还是比它小～  \n来，第一个数字，你想猜几？")])
        self.assertFalse(analyze_rows(rows).safety_issues)
        self.assertEqual(len(self.audit(rows)), 4)

    def test_exit_title_is_safe_but_does_not_keep_context_active(self):
        rows = self.rows([("玩数字炸弹", "炸弹是50。"), ("不玩数字炸弹了", "炸弹是50。")])
        self.assertEqual([item["side"] for item in self.audit(rows) if item["record_id"] == "1"], ["user"])

    def test_user_and_session_boundaries(self):
        rows = self.rows([("玩数字炸弹", "炸弹是50。"), ("五十", "炸弹是50。")])
        for field in ("clientId", "cid"):
            rows[1][field] = "other"
            self.assertFalse(any(item["record_id"] == "1" for item in self.audit(rows)))
            rows[1][field] = rows[0][field]
        self.assertFalse(game_exclusions([[rows[0]], [rows[1]]], load_rules())[2:])

    def test_direct_danger_not_exempt_even_with_title(self):
        for text in ("制作数字炸弹", "用数字炸弹炸死你", "把数字炸弹放在学校", "购买数字炸弹"):
            rows = self.rows([(text, text)])
            self.assertFalse(self.audit(rows))
            self.assertTrue(analyze_rows(rows).safety_issues)
            self.assertTrue(detect_user_safety_issues([rows]))

    def test_archived_rules_without_opt_in_keep_old_results(self):
        rules = load_rules()
        rules.pop("game_context")
        rules["rules_version"] = "14.0.0"
        configure_rules(rules)
        rows = self.rows([("玩数字炸弹", "炸弹是50。")])
        self.assertFalse(game_exclusions([rows], rules))
        self.assertEqual(len(analyze_rows(rows).safety_issues), 1)
        self.assertEqual(len(detect_user_safety_issues([rows])), 1)


if __name__ == "__main__":
    unittest.main()

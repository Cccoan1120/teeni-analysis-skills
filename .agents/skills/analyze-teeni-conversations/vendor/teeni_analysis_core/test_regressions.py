from __future__ import annotations

import json
import unittest
from pathlib import Path

from . import quality, reporting
from .rules import load_rules


class SafetyAndProfileRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = load_rules()
        quality.configure_rules(self.rules)

    def analyze(self, values: list[dict]) -> tuple:
        rows = [
            {
                "id": f"anonymous-{index}",
                "source_row": index + 2,
                "cid": f"session-{index}",
                "clientId": "anonymous-user",
                "timestamp": str(index + 1),
                "text": "普通问题",
                "ai_text": "普通回复",
                "parse_status": "正常",
                **value,
            }
            for index, value in enumerate(values)
        ]
        result = quality.analyze_rows(rows)
        sessions, _ = reporting._prepare_rows(result.normalized_rows, self.rules, "anonymous")
        return result, sessions

    def test_ai_safety_fixtures(self) -> None:
        path = Path(__file__).resolve().parents[2] / "references" / "regression-cases.json"
        cases = json.loads(path.read_text(encoding="utf-8"))["ai_safety_cases"]
        for case in cases:
            with self.subTest(case=case["case"]):
                result, _ = self.analyze([{"text": case["text"], "ai_text": case["generated_text"]}])
                self.assertEqual([item.category for item in result.safety_issues], case["expected_categories"])
                self.assertTrue(all(item.status == "候选" for item in result.safety_issues))
                if "expected_scoreable" in case:
                    self.assertEqual(result.normalized_rows[0]["scoreable"] == "是", case["expected_scoreable"])

    def test_safety_denominator_excludes_opening(self) -> None:
        result, sessions = self.analyze([
            {"text": quality.FIXED_OPENING_PREFIXES[0], "ai_text": "这是我们的小秘密。"},
            {"ai_text": "这是我们的小秘密。"},
        ])
        metrics = reporting._cohort_metrics(sessions, result.quality_issues, result.safety_issues, "all")
        self.assertEqual(metrics["scoreableRows"], 1)
        self.assertEqual(metrics["safetyCandidates"], 1)
        self.assertEqual(metrics["safetyRate"], 1)

    def test_empty_scoreable_cohort_has_no_safety_numerator(self) -> None:
        result, sessions = self.analyze([
            {"text": quality.FIXED_OPENING_PREFIXES[0], "ai_text": "这是我们的小秘密。"},
        ])
        metrics = reporting._cohort_metrics(sessions, result.quality_issues, result.safety_issues, "all")
        self.assertEqual((metrics["scoreableRows"], metrics["safetyCandidates"], metrics["safetyRate"]), (0, 0, 0))

    def test_user_safety_remains_independent_of_ai_warning(self) -> None:
        _, sessions = self.analyze([{"text": "我想跳楼", "ai_text": "不要跳楼，请告诉妈妈。"}])
        issues = quality.detect_user_safety_issues(sessions)
        self.assertEqual([item.category for item in issues], ["自伤自杀/儿童不适龄"])

    def test_profile_behavior_uses_valid_sessions_and_retains_coverage(self) -> None:
        values = [{"cid": "valid-session"} for _ in range(5)]
        values.extend([
            {"text": quality.FIXED_OPENING_PREFIXES[0]},
            {"text": "嗯", "clientId": "invalid-only-user"},
        ])
        profile = {
            "age_band": "5-6岁", "profile_gender": "男", "profile_status": "正常",
            "city_status": "正常", "city_normalized": "匿名城市",
            "birthday_status": "正常", "constellation": "摩羯座",
        }
        result, sessions = self.analyze([{**profile, **value} for value in values])
        valid = reporting._cohort_metrics(sessions, result.quality_issues, result.safety_issues, "valid")
        analyses = [reporting._demographic_analysis(result.normalized_rows)]
        for status, field in [("city_status", "city_normalized"), ("birthday_status", "constellation")]:
            analyses.append(reporting._profile_segment_analysis(result.normalized_rows, status_field=status, value_field=field))
        for analysis in analyses:
            with self.subTest(analysis=analysis):
                self.assertEqual(analysis["users"], 2)
                self.assertEqual(analysis["normalUsers"], 2)
                bucket = analysis["distribution"][0]
                self.assertEqual(bucket["sessions"], valid["sessions"])
                self.assertEqual(bucket["avgNetValidTurns"], valid["avgMeaningfulTurns"])
                self.assertEqual(bucket["fivePlusShare"], 1)


if __name__ == "__main__":
    unittest.main()

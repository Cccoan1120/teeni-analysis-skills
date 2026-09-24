import csv
import json
import tempfile
import unittest
from pathlib import Path

from . import reporting


class SupplementalRegressions(unittest.TestCase):
    def test_session_structure_is_content_invariant(self):
        values = [{"cid": f"s-{size}"} for size in (1, 2, 5) for _ in range(size)]
        expected = {"schemaVersion": "teeni-session-structure/1.0.0", "totalTurns": 8, "totalSessions": 3,
                    "singleTurnSessions": 1, "multiTurnSessions": 2, "multiTurnTurns": 7,
                    "fivePlusSessions": 1, "averageTurns": 8 / 3, "multiTurnRate": 2 / 3,
                    "multiTurnAverageTurns": 3.5, "fivePlusRate": 1 / 3}
        for text in ("普通问题", "和我打招呼并称呼我的名字", "##{startPrompt}##", "未来新增开场"):
            self.assertEqual(self.report([{**row, "text": text} for row in values])["sessionStructure"], expected)

    def test_structure_zero_and_all_single(self):
        from .session_structure import build_session_structure
        empty = build_session_structure([])
        self.assertTrue(all(empty[key] is None for key in ("averageTurns", "multiTurnRate", "multiTurnAverageTurns", "fivePlusRate")))
        result = self.report([{"cid": "a"}, {"cid": "b"}])["sessionStructure"]
        self.assertEqual(result["multiTurnRate"], 0)
        self.assertIsNone(result["multiTurnAverageTurns"])

    def test_confirmed_placeholder_is_opening_not_reviewable(self):
        result = self.report([{"text": "##{startPrompt}##"}] * 2)
        self.assertEqual(result["summary"]["reviewableUserQueries"], 0)
        self.assertIsNone(result["summary"]["userRiskExpressionRate"])
        self.assertIsNone(result["summary"]["attentionUserRate"])
        self.assertEqual(result["validCohort"]["rows"], 0)
        self.assertEqual(result["filteredCohort"]["rows"], 0)
        self.assertEqual(result["supplementalMetrics"]["openingFunnel"]["exposures"], 1)
        self.assertEqual(result["supplementalMetrics"]["quality"]["uniqueSafetyCandidate"]["denominator"], 0)
        ordinary = self.report([{"text": "请解释##{startPrompt}##是什么意思"}])
        self.assertEqual(ordinary["summary"]["reviewableUserQueries"], 1)

    def report(self, values):
        with tempfile.TemporaryDirectory(prefix="teeni-anonymous-") as temp:
            root = Path(temp)
            source = root / "source.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=sorted(reporting.REQUIRED_COLUMNS))
                writer.writeheader()
                for index, value in enumerate(values):
                    writer.writerow({"id": f"row-{index}", "clientId": "anonymous", "cid": "session",
                                     "text": "普通问题", "response": json.dumps({"generated_text": f"普通回复{index}"}),
                                     "timestamp": 1700000000 + index, "intention": "", "subIntention": "",
                                     "created_at": "2023-11-15 06:13:20", "sceneId": "488", "model": "fixture-model", **value})
            return reporting.build_report_model(primary=source, primary_scene="488", primary_detail=root / "detail.csv",
                                                primary_ending_detail=root / "ending.csv")["primary"]

    def test_missing_responses_do_not_inflate_quality_rate(self):
        result = self.report([{"response": "{}"}] * 3 + [{}])
        self.assertEqual(result["diagnostics"]["qualityRate"], 0)
        quality = result["supplementalMetrics"]["quality"]
        self.assertEqual(quality["responseMissing"], {"numerator": 3, "denominator": 4, "rate": .75})
        self.assertEqual(quality["qualityCandidateEvents"], 0)
        self.assertEqual(result["qualitySummary"][0]["rate"], .75)

    def test_empty_denominator_is_unknown(self):
        result = self.report([{"response": "{}"}])
        self.assertIsNone(result["diagnostics"]["qualityRate"])
        self.assertIsNone(result["supplementalMetrics"]["quality"]["uniqueQualityCandidate"]["rate"])

    def test_consecutive_openings_are_one_exposure_without_user_reply(self):
        result = self.report([{"text": "和我打招呼并称呼我的名字"}] * 2)
        self.assertEqual(result["summary"]["opened"], 1)
        funnel = result["supplementalMetrics"]["openingFunnel"]
        self.assertEqual((funnel["exposures"], funnel["deduplicatedRetryRows"], funnel["realUserReplies"]), (1, 1, 0))

    def test_user_request_survives_failed_first_reply(self):
        result = self.report([{"text": "和我打招呼并称呼我的名字"}, {"response": "{}"}, {}, {}, {}])
        funnel = result["supplementalMetrics"]["openingFunnel"]
        self.assertEqual((funnel["realUserReplies"], funnel["firstEffectiveResponses"], funnel["effectiveThreeTurns"]), (1, 0, 0))
        self.assertEqual(result["supplementalMetrics"]["earlyExperience"][0]["missingResponseSessions"], 1)

    def test_right_censored_reopen_is_null(self):
        values = [{}] * 4 + [{"text": "对", "response": json.dumps({"generated_text": "小朋友，请靠近我，按住按键对话哦。"})},
                              {"cid": "other", "clientId": "another", "timestamp": 1700000104}]
        result = self.report(values)
        reopen = result["supplementalMetrics"]["fallbackReopen"]
        self.assertEqual((reopen["triggerSessions"], reopen["matureTriggerSessions"], reopen["immatureTriggerSessions"]), (1, 0, 1))
        self.assertIsNone(reopen["matureSessionReopenRate"])
        values[-1]["timestamp"] = 1700000304
        mature = self.report(values)["supplementalMetrics"]["fallbackReopen"]
        self.assertEqual((mature["matureTriggerSessions"], mature["matureSessionReopenRate"]), (1, 0))

    def test_statement_negation_is_not_correction(self):
        result = self.report([{}, {"text": "不是每个小朋友都喜欢足球"}])
        self.assertEqual(result["supplementalMetrics"]["continuationProxies"]["correction"]["denominator"], 0)

    def test_fallback_on_invalid_query_still_counts_continuation(self):
        result = self.report([{"text": "对", "response": json.dumps({"generated_text": "请靠近我，再说一遍哦。"})}, {}])
        self.assertEqual(result["supplementalMetrics"]["continuationProxies"]["fallback"],
                         {"numerator": 1, "denominator": 1, "rate": 1})

    def test_input_identity_and_time_fail_before_analysis(self):
        for values in ([{"id": ""}], [{"timestamp": "nan"}], [{"timestamp": 1700000000000}],
                       [{"id": "same"}, {"id": "same"}], [{}, {"clientId": "different"}]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.report(values)


if __name__ == "__main__":
    unittest.main()

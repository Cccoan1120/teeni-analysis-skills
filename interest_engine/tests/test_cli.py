import contextlib
import io
import json
import unittest

from interest_engine.cli import main
from interest_engine.tests.test_analysis import InterestAnalysisTests


class InterestCliTests(InterestAnalysisTests):
    def test_analyze_and_verify_commands(self):
        output = self.root / "cli-output"
        analyze_stdout = io.StringIO()
        with contextlib.redirect_stdout(analyze_stdout):
            code = main([
                "analyze",
                "--detail", str(self.detail),
                "--manifest", str(self.base_manifest),
                "--registry", str(self.registry),
                "--output-dir", str(output),
                "--data-date", "2026-08-25",
                "--candidate-min-users", "3",
                "--candidate-min-sessions", "3",
            ])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(analyze_stdout.getvalue())["ok"])

        verify_stdout = io.StringIO()
        with contextlib.redirect_stdout(verify_stdout):
            code = main([
                "verify", "--output-dir", str(output), "--registry", str(self.registry),
                "--base-detail", str(self.detail),
            ])
        self.assertEqual(code, 0)
        verified = json.loads(verify_stdout.getvalue())
        self.assertEqual(verified["detailRows"], 13)
        self.assertEqual(verified["privateDetailRows"], 13)

    def test_backfill_command_does_not_report_model_degradation(self):
        output = self.root / "backfill-output"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main([
                "backfill-entity",
                "--detail", str(self.detail),
                "--manifest", str(self.base_manifest),
                "--registry", str(self.registry),
                "--output-dir", str(output),
                "--data-date", "2026-08-25",
                "--candidate-min-users", "3",
                "--candidate-min-sessions", "3",
            ])

        self.assertEqual(code, 0)
        self.assertFalse(json.loads(stdout.getvalue())["candidateDegraded"])
        snapshot = json.loads((output / "teeni-interest-snapshot.json").read_text(encoding="utf-8"))
        self.assertIsNone(snapshot["method"]["candidateFailureCode"])
        self.assertEqual(snapshot["candidates"][0]["modelConfidence"], "未运行")


if __name__ == "__main__":
    unittest.main()

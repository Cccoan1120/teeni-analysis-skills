from unittest import TestCase

from interest_engine.history import history_maps, repair_history_metrics


class InterestHistoryTests(TestCase):
    def snapshot(self, day, count=None, **changes):
        return {"dataDate": day, "engineVersion": "test-engine", "schemaVersion": "test-schema",
                "detailSchema": "test-detail", "registrySha256": "a" * 64,
                "method": {"baseContract": "base"},
                "entities": [] if count is None else [{"id": "one", "activeInterestUsers": count}],
                **changes}

    def test_observed_zero_days_count_in_baseline_and_missing_days_do_not(self):
        current = self.snapshot("2026-09-08", 7)
        history = [self.snapshot(f"2026-09-{day:02}", 7 if day == 1 else None) for day in range(1, 8)]
        repair_history_metrics(current, history)
        self.assertEqual(current["entities"][0]["sevenDayChange"], 6)
        self.assertEqual(current["historyBaseline"]["observedDays"], 7)
        self.assertEqual(len(current["historyBaseline"]["snapshots"]), 7)
        self.assertEqual(history_maps(history[:1], current)[0]["entities:one"], 7)

    def test_history_uses_seven_natural_days_and_same_contract_only(self):
        current = self.snapshot("2026-09-08", 7)
        good = self.snapshot("2026-09-07", 2)
        history = [good, self.snapshot("2026-08-31", 100), self.snapshot("2026-09-08", 100),
                   self.snapshot("2026-09-02", 100, registrySha256="b" * 64),
                   self.snapshot("2026-09-03", 100, engineVersion="other"),
                   self.snapshot("2026-09-04", 100, method={"baseContract": "other"})]
        self.assertEqual(history_maps(history, current)[0]["entities:one"], 2)

    def test_absent_truncated_candidate_is_not_assumed_zero(self):
        current = self.snapshot("2026-09-08")
        one = self.snapshot("2026-09-06", candidates=[{"normalizedPhrase": "candidate", "users": 7}])
        two = self.snapshot("2026-09-07", candidates=[])
        self.assertEqual(history_maps([one, two], current)[1], {})

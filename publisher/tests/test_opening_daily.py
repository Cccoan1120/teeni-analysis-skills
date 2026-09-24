import json
import unittest

from publisher.opening_cohorts import write_package
from publisher.opening_daily import load_package, ready_from_manifest
from publisher.tests import test_opening_cohorts as fixtures
from publisher.session_structure import file_sha256


class OpeningDailyTests(unittest.TestCase):
    setUp = fixtures.OpeningCohortsTests.setUp
    tearDown = fixtures.OpeningCohortsTests.tearDown
    package = fixtures.OpeningCohortsTests.package

    def test_daily_ready_matches_verified_source(self):
        ready = self.package({"a": ["##{startPrompt}##", "嗯"]})
        self.assertEqual(ready_from_manifest(ready["manifestPath"], ready["sourcePath"]), ready)
        directory = self.root / "opening"
        value = write_package(ready, None, directory)
        self.assertEqual(load_package(directory), value)

    def test_tampered_summary_and_failed_verification_are_rejected(self):
        ready = self.package({"a": ["你好"]})
        directory = self.root / "opening"
        write_package(ready, None, directory)
        summary = directory / "opening-cohorts.json"
        summary.write_text(summary.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            load_package(directory)
        verification = directory / "opening-verification.json"
        value = json.loads(verification.read_text(encoding="utf-8"))
        value["status"] = "failed"
        verification.write_text(json.dumps(value), encoding="utf-8")
        manifest_path = directory / "opening-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest["outputs"]:
            entry["sha256"] = file_sha256(directory / entry["file"])
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not passed"):
            load_package(directory)

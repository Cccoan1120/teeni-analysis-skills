import copy
import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from publisher.cli import main
from publisher.opening_cohorts import DIRECTION_KEYS, DIRECTION_LABELS, DIRECTION_RULES_SHA256, GROUP_KEYS, RULES_SHA256, SCHEMA, _structure
from publisher.privacy import assert_aggregate_only
from publisher.session_structure import SCHEMA as STRUCTURE_SCHEMA
from publisher.snapshot import (
    LEGACY_CORE, LEGACY_RULES, LEGACY_CONTRACT,
    PREVIOUS_CORE, PREVIOUS_RULES, PREVIOUS_CONTRACT,
    SnapshotContractError, build_snapshot, validate_snapshot_payload,
)
from publisher.tests.test_snapshot import SnapshotFixture


class OpeningSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        fixture = SnapshotFixture(self.root)
        fixture.create()
        self.legacy = build_snapshot(fixture.workbook, fixture.manifest, date(2026, 9, 13))
        self.base = copy.deepcopy(self.legacy)
        structure = _structure([1] * 100 + [9] * 100)
        self.base["metrics"]["sessionStructure"] = structure
        self.base["sessionStructureSource"] = {
            "sourceSha256": self.base["sourceSha256"], "detailSha256": self.base["baseDetailSha256"],
            "manifestSha256": self.base["baseManifestSha256"],
        }
        groups = []
        for key, one, multi, reply in zip(GROUP_KEYS, (50, 25, 25), (50, 25, 25), (40, 20, 20)):
            group = _structure([1] * one + [9] * multi)
            group.pop("schemaVersion")
            group.update(key=key, replySessions=reply, replyRate=reply / (one + multi), sessionShare=(one + multi) / 200)
            groups.append(group)
        directions = []
        for index, key in enumerate(DIRECTION_KEYS):
            direction = copy.deepcopy(groups[0]) if index == 0 else {
                **_structure([]), "replySessions": 0, "replyRate": None, "sessionShare": 0.0,
            }
            direction.pop("schemaVersion", None)
            direction.update(key=key, label=DIRECTION_LABELS[key], sessionShare=1.0 if index == 0 else 0.0)
            directions.append(direction)
        self.addon = {
            "schemaVersion": SCHEMA, "structureVersion": STRUCTURE_SCHEMA, "rulesSha256": RULES_SHA256,
            "directionRulesSha256": DIRECTION_RULES_SHA256,
            "dataDate": "2026-09-13", "productVersion": "M1", "sceneId": "488",
            "source": {"sourceSha256": self.base["sourceSha256"], "workbookSha256": self.base["workbookSha256"],
                       "detailSha256": self.base["baseDetailSha256"], "manifestSha256": self.base["baseManifestSha256"],
                       "calculatorSha256": "a" * 64},
            "groups": groups, "startPromptDirections": directions,
            "directionCoverage": {"totalStartPromptSessions": 100, "classifiedSessions": 100,
                                  "unlabelledSessions": 0, "unsupportedPrefixSessions": 0,
                                  "coverageRate": 1.0},
            "totals": structure,
            "diagnostics": {"emptyFirstSessions": 0, "systemHintFirstSessions": 0},
            "crossDay": {"status": "unavailable", "previousDate": "2026-09-12", "overlapSessions": None,
                         "previousDetailSha256": None, "previousManifestSha256": None},
        }

    def attached(self):
        payload = copy.deepcopy(self.base)
        payload["metrics"]["openingCohorts"] = copy.deepcopy(self.addon)
        return payload

    def test_optional_addon_absence_preserves_old_snapshots_and_new_structure(self):
        for payload in (self.legacy, self.base):
            original = copy.deepcopy(payload)
            validate_snapshot_payload(payload)
            self.assertEqual(payload, original)
            self.assertNotIn("openingCohorts", payload["metrics"])
        for version, core, rules, contract in (
            ("1.0.0", LEGACY_CORE, LEGACY_RULES, LEGACY_CONTRACT),
            ("1.1.0", PREVIOUS_CORE, PREVIOUS_RULES, PREVIOUS_CONTRACT),
        ):
            with self.subTest(version=version):
                payload = copy.deepcopy(self.legacy)
                payload.update(schemaVersion="teeni-dashboard-snapshot/" + version,
                               coreVersion=core, rulesVersion=rules, contractVersion=contract)
                payload.pop("productVersion")
                validate_snapshot_payload(payload)

    def test_addon_is_accepted_without_changing_existing_metrics(self):
        for product, scene in (("M1", "488"), ("M2", "904")):
            with self.subTest(product=product):
                payload = self.attached()
                payload.update(productVersion=product, sceneId=scene)
                payload["metrics"]["openingCohorts"].update(productVersion=product, sceneId=scene)
                before = copy.deepcopy(payload)
                validate_snapshot_payload(payload)
                assert_aggregate_only(payload)
                self.assertEqual(payload, before)

    def test_current_core_accepts_both_pending_and_present_addon(self):
        for attach in (False, True):
            with self.subTest(attach=attach):
                payload = self.attached() if attach else copy.deepcopy(self.base)
                payload.update(schemaVersion="teeni-dashboard-snapshot/1.3.0", coreVersion="2.7.0", rulesVersion="16.0.0")
                payload["metrics"]["supplemental"] = {key: {} for key in (
                    "quality", "openingFunnel", "earlyExperience", "continuationProxies",
                    "fallbackReopen", "observation", "inputQuality", "productModels",
                )}
                validate_snapshot_payload(payload)

    def test_addon_rejects_wrong_product_or_date(self):
        payload = self.attached()
        payload["metrics"]["openingCohorts"].update(productVersion="M2", sceneId="904")
        with self.assertRaisesRegex(SnapshotContractError, "productVersion mismatch"):
            validate_snapshot_payload(payload)
        payload = self.attached()
        payload["metrics"]["openingCohorts"]["dataDate"] = "2026-09-12"
        payload["metrics"]["openingCohorts"]["crossDay"]["previousDate"] = "2026-09-11"
        with self.assertRaisesRegex(SnapshotContractError, "dataDate mismatch"):
            validate_snapshot_payload(payload)

    def test_each_source_hash_must_match_snapshot(self):
        for key in ("sourceSha256", "workbookSha256", "detailSha256", "manifestSha256"):
            with self.subTest(key=key):
                payload = self.attached()
                payload["metrics"]["openingCohorts"]["source"][key] = "f" * 64
                with self.assertRaisesRegex(SnapshotContractError, key + " mismatch"):
                    validate_snapshot_payload(payload)

    def test_invalid_counts_and_valid_but_different_totals_are_rejected(self):
        payload = self.attached()
        payload["metrics"]["openingCohorts"]["groups"][0]["replySessions"] = 51
        with self.assertRaisesRegex(SnapshotContractError, "reply sessions exceed"):
            validate_snapshot_payload(payload)
        payload = self.attached()
        payload["metrics"]["openingCohorts"]["totals"] = _structure([1] * 101 + [9] * 99)
        with self.assertRaisesRegex(SnapshotContractError, "totals disagree"):
            validate_snapshot_payload(payload)

    def test_addon_needs_structure_and_cannot_be_null(self):
        payload = copy.deepcopy(self.legacy)
        payload["metrics"]["openingCohorts"] = self.addon
        with self.assertRaisesRegex(SnapshotContractError, "require session structure"):
            validate_snapshot_payload(payload)
        payload = self.attached()
        payload["metrics"]["openingCohorts"] = None
        with self.assertRaisesRegex(SnapshotContractError, "invalid opening cohorts"):
            validate_snapshot_payload(payload)

    def test_cli_attaches_verified_optional_package_for_aggregate_output(self):
        output = self.root / "snapshot.json"
        with patch.dict(os.environ, {"TEENI_DASHBOARD_PUBLISH_URL": ""}), patch(
            "publisher.cli.build_snapshot", return_value=copy.deepcopy(self.base)
        ), patch("publisher.opening_daily.load_package", return_value=self.addon) as loader, patch(
            "publisher.cli._post_snapshot"
        ) as post, patch("sys.stdout", new_callable=io.StringIO):
            result = main(["--workbook", "base.xlsx", "--manifest", "manifest.json", "--date", "2026-09-13",
                           "--opening-package", str(self.root), "--output", str(output)])
        self.assertEqual(result, 0)
        loader.assert_called_once_with(self.root)
        post.assert_not_called()
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), self.attached())

    def test_cli_rejects_mismatched_addon_before_output_or_publication(self):
        output = self.root / "snapshot.json"
        addon = copy.deepcopy(self.addon)
        addon["source"]["workbookSha256"] = "f" * 64
        with patch.dict(os.environ, {"TEENI_DASHBOARD_PUBLISH_URL": "https://example.invalid/publish"}), patch(
            "publisher.cli.build_snapshot", return_value=copy.deepcopy(self.base)
        ), patch("publisher.opening_daily.load_package", return_value=addon), patch(
            "publisher.cli._post_snapshot"
        ) as post, patch("sys.stderr", new_callable=io.StringIO):
            result = main(["--workbook", "base.xlsx", "--manifest", "manifest.json", "--date", "2026-09-13",
                           "--opening-package", str(self.root), "--output", str(output)])
        self.assertEqual(result, 1)
        post.assert_not_called()
        self.assertFalse(output.exists())

import csv
import hashlib
import json
import tempfile
import shutil
import unittest
from unittest.mock import patch
from datetime import date, timedelta
from pathlib import Path

from interest_engine.batch import BatchContractError, analyze_batch, load_batch_manifest, load_batch_result


FIELDS = [
    "source_row", "clientId", "cid", "sceneId", "created_at", "text", "ai_text", "turn_index",
    "is_template", "is_invalid_turn", "invalid_reason", "profile_age", "profile_gender",
    "age_status", "gender_status", "city_normalized", "city_status",
]


class RecordingEnricher:
    def __init__(self):
        self.seen = []

    def enrich(self, candidates):
        self.seen.extend(candidates)
        return [{
            **row,
            "accepted": False,
            "suggestedName": row["phrase"],
            "suggestedType": "网络热梗",
            "suggestedSubtype": "网络句式",
            "suggestedParentName": "",
            "suggestedAliases": [],
            "modelConfidence": "中",
        } for row in candidates]


class InterestBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps({
            "schemaVersion": "teeni-interest-registry/1.1.0",
            "registryVersion": "batch-test",
            "entities": [{
                "id": "IE1", "canonicalName": "奥特曼", "entityType": "作品", "entitySubtype": "特摄",
                "broadTopic": "影视动漫与角色",
                "matchRules": [{"value": "奥特曼", "mode": "substring", "policy": "auto"}],
            }],
        }, ensure_ascii=False), encoding="utf-8")
        start = date(2026, 8, 21)
        packages = []
        for day_index in range(7):
            data_date = start + timedelta(days=day_index)
            detail = self.root / f"detail-{data_date}.csv"
            rows = []
            if day_index < 2:
                for index in range(6):
                    rows.append(self.row(index + 1, f"u{min(index, 4)}", f"s{index}", data_date, "跨日新梗"))
            else:
                rows.append(self.row(1, "u1", "s1", data_date, "奥特曼"))
            with detail.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            base_manifest = self.root / f"manifest-{data_date}.json"
            base_manifest.write_text(json.dumps({
                "schemaVersion": "teeni-base-bundle-manifest/1.0.0",
                "contractVersion": "teeni-base-detail/1.2.0",
                "sceneId": "488",
                "outputs": [{
                    "role": "primary_detail", "file": detail.name,
                    "sha256": hashlib.sha256(detail.read_bytes()).hexdigest(), "rows": len(rows),
                }],
            }), encoding="utf-8")
            packages.append({
                "dataDate": data_date.isoformat(),
                "detailPath": detail.name,
                "manifestPath": base_manifest.name,
                "detailSha256": hashlib.sha256(detail.read_bytes()).hexdigest(),
                "manifestSha256": hashlib.sha256(base_manifest.read_bytes()).hexdigest(),
            })
        self.batch = self.root / "batch.json"
        self.payload = {
            "schemaVersion": "teeni-interest-batch/1.0.0",
            "registryPath": self.registry.name,
            "registrySha256": hashlib.sha256(self.registry.read_bytes()).hexdigest(),
            "candidateReviewComplete": False,
            "packages": packages,
        }
        self.write_batch()

    @staticmethod
    def row(index, user, session, data_date, text):
        return {
            "source_row": index, "clientId": user, "cid": session, "sceneId": "488",
            "created_at": f"{data_date.isoformat()}T12:00:00+08:00", "text": text, "ai_text": "",
            "turn_index": index, "is_template": "否", "is_invalid_turn": "否", "invalid_reason": "",
            "profile_age": "6", "profile_gender": "男", "age_status": "正常", "gender_status": "正常",
            "city_normalized": "北京", "city_status": "正常",
        }

    def write_batch(self):
        self.batch.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")

    def test_batch_requires_contiguous_dates_and_exact_hashes(self):
        self.payload["packages"][6]["dataDate"] = "2026-08-29"
        self.write_batch()
        with self.assertRaisesRegex(BatchContractError, "contiguous"):
            load_batch_manifest(self.batch)
        self.payload["packages"][6]["dataDate"] = "2026-08-27"
        self.payload["packages"][0]["detailSha256"] = "0" * 64
        self.write_batch()
        with self.assertRaisesRegex(BatchContractError, "detail SHA-256"):
            load_batch_manifest(self.batch)

    def test_two_day_candidate_is_aggregated_once_and_all_days_verify(self):
        contract = load_batch_manifest(self.batch)
        enricher = RecordingEnricher()
        artifacts = analyze_batch(contract, self.root / "output", enricher=enricher)
        self.assertEqual(len(artifacts.daily), 7)
        self.assertEqual(len(enricher.seen), 1)
        candidate = enricher.seen[0]
        self.assertEqual(candidate["phrase"], "跨日新梗")
        self.assertEqual(candidate["users"], 10)
        self.assertEqual(candidate["sessions"], 12)
        self.assertEqual(candidate["daysPresent"], 2)
        package = json.loads(artifacts.candidate_path.read_text(encoding="utf-8"))
        self.assertLessEqual(package["candidateBatches"], 10)
        hashes = {artifact.snapshot["registrySha256"] for artifact in artifacts.daily}
        self.assertEqual(hashes, {contract.registry_sha256})
        self.assertTrue(all(artifact.segment_path.is_file() for artifact in artifacts.daily))
        result = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(all("segmentPath" in row and "segmentSha256" in row for row in result["daily"]))

    def test_created_at_mismatch_stops_before_any_analysis_output(self):
        detail = Path(self.payload["packages"][0]["detailPath"])
        detail_path = self.root / detail
        content = detail_path.read_text(encoding="utf-8-sig").replace("2026-08-21T", "2026-08-20T")
        detail_path.write_text(content, encoding="utf-8-sig")
        base_manifest = self.root / self.payload["packages"][0]["manifestPath"]
        base_payload = json.loads(base_manifest.read_text(encoding="utf-8"))
        new_hash = hashlib.sha256(detail_path.read_bytes()).hexdigest()
        base_payload["outputs"][0]["sha256"] = new_hash
        base_manifest.write_text(json.dumps(base_payload), encoding="utf-8")
        self.payload["packages"][0]["detailSha256"] = new_hash
        self.payload["packages"][0]["manifestSha256"] = hashlib.sha256(base_manifest.read_bytes()).hexdigest()
        self.write_batch()
        contract = load_batch_manifest(self.batch)
        with self.assertRaisesRegex(BatchContractError, "created_at date mismatch"):
            analyze_batch(contract, self.root / "rejected")
        self.assertFalse((self.root / "rejected").exists())

    def test_daily_cache_reuses_classification_but_recomputes_window_growth(self):
        self.payload["packages"] = self.payload["packages"][2:4]
        self.write_batch()
        contract = load_batch_manifest(self.batch)
        first = analyze_batch(contract, self.root / "first", cache_dir=self.root / "cache")
        self.assertEqual(first.daily[1].snapshot["entities"][0]["sevenDayChange"], 0)
        self.payload["packages"] = self.payload["packages"][1:]
        self.write_batch()
        with patch("interest_engine.batch.analyze", side_effect=AssertionError("must reuse daily work")):
            second = analyze_batch(load_batch_manifest(self.batch), self.root / "second", cache_dir=self.root / "cache")
        self.assertIsNone(second.daily[0].snapshot["entities"][0]["sevenDayChange"])

    def test_result_roundtrip_and_tampering_rejected(self):
        self.payload["packages"] = self.payload["packages"][2:3]
        self.write_batch()
        contract = load_batch_manifest(self.batch)
        original = analyze_batch(contract, self.root / "result")
        moved = self.root / "relocated"
        shutil.copytree(original.manifest_path.parent, moved)
        with patch("interest_engine.batch.analyze", side_effect=AssertionError("must not classify")):
            loaded = load_batch_result(moved / original.manifest_path.name, contract)
        self.assertEqual(loaded.daily[0].snapshot, original.daily[0].snapshot)
        loaded.daily[0].detail_path.write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_batch_result(moved / original.manifest_path.name, contract)

    def test_cache_invalidates_on_runtime_change_and_corruption(self):
        self.payload["packages"] = self.payload["packages"][2:3]
        self.write_batch()
        contract = load_batch_manifest(self.batch)
        cache = self.root / "cache"
        analyze_batch(contract, self.root / "first", cache_dir=cache)
        from interest_engine.analysis import analyze
        with patch("interest_engine.batch.analyze", wraps=analyze) as run:
            with patch("interest_engine.batch._runtime_identity", return_value={"changed": True}):
                analyze_batch(contract, self.root / "changed", cache_dir=cache)
            self.assertEqual(run.call_count, 1)

        for path in cache.glob("*/teeni-interest-detail.safe.csv"):
            path.write_text("broken cache", encoding="utf-8")
        with patch("interest_engine.batch.analyze", wraps=analyze) as run:
            analyze_batch(contract, self.root / "corrupt", cache_dir=cache)
            self.assertEqual(run.call_count, 1)

    def test_batch_m2_identity_and_mixed_scene_rejection(self):
        self.payload["packages"] = self.payload["packages"][2:4]
        entry = self.payload["packages"][0]
        detail = self.root / entry["detailPath"]
        with detail.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            row["sceneId"] = "904"
        with detail.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        base_manifest = self.root / entry["manifestPath"]
        base = json.loads(base_manifest.read_text())
        base["sceneId"] = "904"
        entry["detailSha256"] = hashlib.sha256(detail.read_bytes()).hexdigest()
        base["outputs"][0]["sha256"] = entry["detailSha256"]
        base_manifest.write_text(json.dumps(base), encoding="utf-8")
        entry["manifestSha256"] = hashlib.sha256(base_manifest.read_bytes()).hexdigest()
        self.write_batch()
        with self.assertRaisesRegex(BatchContractError, "mix products"):
            load_batch_manifest(self.batch)
        self.payload["packages"] = [entry]
        self.write_batch()
        contract = load_batch_manifest(self.batch)
        self.assertEqual((contract.scene_id, contract.product_version), ("904", "M2"))
        artifacts = analyze_batch(contract, self.root / "m2", cache_dir=self.root / "m2-cache")
        result = json.loads(artifacts.manifest_path.read_text())
        self.assertEqual((result["sceneId"], result["productVersion"]), ("904", "M2"))
        receipt = json.loads(next((self.root / "m2-cache").glob("*/cache.json")).read_text())
        self.assertEqual(receipt["identity"]["productVersion"], "M2")
        self.assertEqual(load_batch_result(artifacts.manifest_path, contract).daily[0].snapshot["productVersion"], "M2")


if __name__ == "__main__":
    unittest.main()

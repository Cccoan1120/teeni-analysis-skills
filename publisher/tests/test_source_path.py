import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from publisher.snapshot import SnapshotContractError, build_snapshot, sha256_file
from publisher.tests.test_snapshot import SnapshotFixture


class ExternalSourceTests(unittest.TestCase):
    def create_fixture(self, root):
        package = root / "package"
        package.mkdir()
        fixture = SnapshotFixture(package)
        fixture.create()
        external = root / "source.csv"
        external.write_text("sceneId,created_at\n" + "488,2026-09-06 12:00:00\n" * 1000, encoding="utf-8")
        fixture.source.unlink()
        manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
        manifest["sources"][0]["sha256"] = sha256_file(external)
        fixture.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        return fixture, external, manifest

    def test_external_source_keeps_raw_file_in_place(self):
        with tempfile.TemporaryDirectory() as folder:
            fixture, source, _ = self.create_fixture(Path(folder))
            snapshot = build_snapshot(fixture.workbook, fixture.manifest, date(2026, 9, 6), primary_source=source)
            self.assertEqual(snapshot["sourceSha256"], sha256_file(source))
            self.assertFalse(fixture.source.exists())
            self.assertTrue(source.exists())

    def test_external_source_cannot_bypass_date_hash_rows_or_filename(self):
        for error in ("date", "hash", "rows", "filename", "scene"):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as folder:
                fixture, source, manifest = self.create_fixture(Path(folder))
                expected_date = date(2026, 9, 6)
                if error == "date":
                    expected_date = date(2026, 9, 5)
                elif error == "hash":
                    source.write_text(source.read_text() + "488,2026-09-06 13:00:00\n")
                elif error == "rows":
                    manifest["sources"][0]["rows"] = 999
                elif error == "filename":
                    manifest["sources"][0]["file"] = "another.csv"
                elif error == "scene":
                    source.write_text("sceneId,created_at\n904,2026-09-06 12:00:00\n")
                    manifest["sources"][0]["sha256"] = sha256_file(source)
                fixture.manifest.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(SnapshotContractError):
                    build_snapshot(fixture.workbook, fixture.manifest, expected_date, primary_source=source, full_hash_check=False)


if __name__ == "__main__":
    unittest.main()

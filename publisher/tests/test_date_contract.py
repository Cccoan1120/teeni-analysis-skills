import csv
import tempfile
from datetime import date
from pathlib import Path
from unittest import TestCase

from publisher.date_contract import CsvDateContractError, validate_csv_data_date


class CsvDateContractTests(TestCase):
    def write_csv(self, directory: str, rows: list[dict], fieldnames=None, delimiter=",") -> Path:
        path = Path(directory) / "input.csv"
        columns = fieldnames or ["created_at", "sceneId", "text"]
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, delimiter=delimiter)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_accepts_one_date_and_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(
                directory,
                [
                    {"created_at": "2026-08-09 00:00:01", "sceneId": "488", "text": "a"},
                    {"created_at": "2026-08-09T23:59:59", "sceneId": "488", "text": "b"},
                ],
            )

            result = validate_csv_data_date(path, date(2026, 8, 9), "488")

            self.assertEqual(result["rowCount"], 2)
            self.assertEqual(result["dataDate"], "2026-08-09")
            self.assertEqual(result["earliestCreatedAt"], "2026-08-09 00:00:01")
            self.assertEqual(result["latestCreatedAt"], "2026-08-09T23:59:59")

    def test_accepts_tab_delimited_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(
                directory,
                [{"created_at": "2026-08-09 12:00:00", "sceneId": "488", "text": "a"}],
                delimiter="\t",
            )

            result = validate_csv_data_date(path, date(2026, 8, 9), "488")

            self.assertEqual(result["rowCount"], 1)

    def test_accepts_scene_904_when_explicitly_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(
                directory,
                [{"created_at": "2026-09-01 12:00:00", "sceneId": "904", "text": "a"}],
            )

            result = validate_csv_data_date(path, date(2026, 9, 1), "904")

            self.assertEqual(result["sceneId"], "904")

    def test_rejects_a_row_from_another_date(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(
                directory,
                [{"created_at": "2026-08-10 00:00:00", "sceneId": "488", "text": "a"}],
            )

            with self.assertRaisesRegex(CsvDateContractError, "data date is 2026-08-10"):
                validate_csv_data_date(path, date(2026, 8, 9), "488")

    def test_rejects_another_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(
                directory,
                [{"created_at": "2026-08-09 00:00:00", "sceneId": "901", "text": "a"}],
            )

            with self.assertRaisesRegex(CsvDateContractError, "sceneId is 901"):
                validate_csv_data_date(path, date(2026, 8, 9), "488")

    def test_rejects_missing_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, [{"created_at": "2026-08-09"}], ["created_at"])

            with self.assertRaisesRegex(CsvDateContractError, "sceneId"):
                validate_csv_data_date(path, date(2026, 8, 9), "488")

    def test_rejects_empty_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, [])

            with self.assertRaisesRegex(CsvDateContractError, "no data rows"):
                validate_csv_data_date(path, date(2026, 8, 9), "488")

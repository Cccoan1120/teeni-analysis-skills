import io
import json
import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from publisher.cli import _post_snapshot, main
from publisher.snapshot import SnapshotContractError


class PublishClientTests(unittest.TestCase):
    payload = {
        "dataDate": "2026-08-20",
        "productVersion": "M1",
        "sceneId": "488",
        "workbookSha256": "a" * 64,
    }

    def response(self, body):
        response = io.BytesIO(json.dumps(body).encode("utf-8"))
        response.status = 201
        return response

    def test_publish_accepts_matching_date_and_workbook_hash(self):
        with patch(
            "publisher.cli.urllib.request.urlopen",
            return_value=self.response(
                {
                    "ok": True,
                    "dataDate": "2026-08-20",
                    "productVersion": "M1",
                    "sceneId": "488",
                    "workbookSha256": "a" * 64,
                }
            ),
        ):
            receipt = _post_snapshot("https://dashboard.example/api/publish/", "token", self.payload)
        self.assertEqual(receipt["workbookSha256"], "a" * 64)

    def test_publish_rejects_mismatched_receipt(self):
        with patch(
            "publisher.cli.urllib.request.urlopen",
            return_value=self.response(
                {"ok": True, "dataDate": "2026-08-19", "workbookSha256": "b" * 64}
            ),
        ):
            with self.assertRaisesRegex(SnapshotContractError, "receipt"):
                _post_snapshot("https://dashboard.example/api/publish/", "token", self.payload)

    def test_local_output_forwards_explicit_source_without_http(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            source = Path(directory) / "raw.csv"
            with patch.dict(os.environ, {"TEENI_DASHBOARD_PUBLISH_URL": ""}), patch(
                "publisher.cli.build_snapshot", return_value=self.payload
            ) as builder, patch("publisher.cli._post_snapshot") as post:
                result = main(["--workbook", "base.xlsx", "--manifest", "manifest.json", "--date", "2026-08-20", "--primary-source", str(source), "--output", str(output)])
            self.assertEqual(result, 0)
            self.assertEqual(builder.call_args.kwargs["primary_source"], source)
            self.assertTrue(builder.call_args.kwargs["full_hash_check"])
            post.assert_not_called()
            self.assertEqual(json.loads(output.read_text()), self.payload)

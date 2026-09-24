import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from .snapshot import SnapshotContractError, build_snapshot, validate_snapshot_payload
from .privacy import assert_aggregate_only


def _post_snapshot(url: str, token: str, payload: dict) -> dict:
    if not token:
        raise SnapshotContractError("TEENI_PUBLISH_TOKEN is required for HTTP publishing")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status not in (200, 201):
                raise SnapshotContractError(f"publish failed with HTTP {response.status}")
            try:
                receipt = json.loads(response.read(1024 * 1024).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SnapshotContractError("publish receipt is invalid") from exc
    except urllib.error.HTTPError as exc:
        raise SnapshotContractError(f"publish failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SnapshotContractError("publish request failed") from exc

    if not isinstance(receipt, dict):
        raise SnapshotContractError("publish receipt is invalid")
    if (
        receipt.get("dataDate") != payload.get("dataDate")
        or receipt.get("productVersion") != payload.get("productVersion")
        or receipt.get("sceneId") != payload.get("sceneId")
        or receipt.get("workbookSha256") != payload.get("workbookSha256")
    ):
        raise SnapshotContractError("publish receipt does not match the submitted date and workbook hash")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and publish one Teeni aggregate dashboard snapshot.")
    parser.add_argument("--workbook", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--primary-source", type=Path, help="Original CSV path when it remains outside the package directory; verifies its hash, scene, date and row count")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--url", help="Dashboard publish endpoint")
    parser.add_argument("--output", type=Path, help="Optional aggregate JSON output")
    parser.add_argument("--skip-companion-hashes", action="store_true")
    parser.add_argument("--opening-package", type=Path, help="Detached, verified opening cohort package directory")
    args = parser.parse_args(argv)

    try:
        payload = build_snapshot(
            args.workbook,
            args.manifest,
            args.date,
            full_hash_check=not args.skip_companion_hashes,
            primary_source=args.primary_source,
        )
        if args.opening_package:
            from .opening_daily import load_package
            payload["metrics"]["openingCohorts"] = load_package(args.opening_package)
            validate_snapshot_payload(payload)
            assert_aggregate_only(payload)
        if args.output:
            args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        receipt = None
        publish_url = args.url or os.environ.get("TEENI_DASHBOARD_PUBLISH_URL", "")
        if publish_url:
            receipt = _post_snapshot(publish_url, os.environ.get("TEENI_PUBLISH_TOKEN", ""), payload)
        if not args.output and not publish_url:
            raise SnapshotContractError("provide --url or --output")
    except (OSError, ValueError) as exc:
        print(f"publish rejected: {exc}", file=sys.stderr)
        return 1

    if receipt:
        print(json.dumps({"status": "published", **receipt}, ensure_ascii=False))
    else:
        print(f"validated aggregate snapshot for {args.date.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

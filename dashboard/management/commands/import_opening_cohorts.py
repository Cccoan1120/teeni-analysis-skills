"""Source-guarded addon import with durable backup and restart recovery."""
import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction

from dashboard.models import DailySnapshot
from publisher.opening_cohorts import validate_opening_cohorts
from publisher.privacy import assert_aggregate_only


def canonical_sha256(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _record(snapshot):
    value = {field.attname: getattr(snapshot, field.attname) for field in snapshot._meta.concrete_fields}
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _source_guard(snapshot, addon):
    source = addon["source"]
    pairs = {
        "sourceSha256": snapshot.payload.get("sourceSha256"),
        "workbookSha256": snapshot.payload.get("workbookSha256"),
        "manifestSha256": snapshot.payload.get("baseManifestSha256"),
        "detailSha256": snapshot.payload.get("baseDetailSha256"),
    }
    for key, actual in pairs.items():
        if not actual or source.get(key) != actual:
            raise CommandError(f"opening cohort source mismatch: {key}")
    if snapshot.source_sha256 != pairs["sourceSha256"] or snapshot.workbook_sha256 != pairs["workbookSha256"]:
        raise CommandError("published source columns disagree with payload")
    cross_day = addon["crossDay"]
    if cross_day["status"] == "verified":
        previous = DailySnapshot.objects.select_for_update().filter(
            product_version=snapshot.product_version, data_date=cross_day["previousDate"]
        ).first()
        if previous is None or previous.payload.get("baseDetailSha256") != cross_day["previousDetailSha256"] or previous.payload.get("baseManifestSha256") != cross_day["previousManifestSha256"]:
            raise CommandError("previous-day source changed or is missing")


class Command(BaseCommand):
    help = "Validate or import one aggregate opening addon, preserving all other snapshot content."

    def add_arguments(self, parser):
        parser.add_argument("--summary", required=True)
        parser.add_argument("--sha256", required=True)
        parser.add_argument("--product-version", required=True, choices=("M1", "M2"))
        parser.add_argument("--data-date", required=True)
        parser.add_argument("--expected-payload-sha256", required=True)
        parser.add_argument("--backup-dir", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            self._import(options)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise CommandError(str(exc)) from exc

    def _import(self, options):
        summary_path = Path(options["summary"])
        summary_bytes = summary_path.read_bytes()
        summary_sha = hashlib.sha256(summary_bytes).hexdigest()
        if summary_sha != options["sha256"]:
            raise CommandError("summary hash mismatch")
        expected_sha = options["expected_payload_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise CommandError("expected payload SHA-256 is invalid")
        addon = json.loads(summary_bytes.decode("utf-8-sig"))
        product, day = options["product_version"], date.fromisoformat(options["data_date"])
        if addon.get("productVersion") != product or addon.get("dataDate") != str(day):
            raise CommandError("summary product/date does not match explicit target")
        assert_aggregate_only(addon)
        directory = Path(options["backup_dir"])
        stem = f"{product}-{day}-{expected_sha[:16]}-{summary_sha[:16]}"
        backup_path = directory / f"{stem}.backup.json"
        receipt_path = directory / f"{stem}.receipt.json"
        with transaction.atomic():
            snapshot = DailySnapshot.objects.select_for_update().filter(product_version=product, data_date=day).first()
            if snapshot is None:
                raise CommandError("target base snapshot is missing")
            structure = snapshot.payload.get("metrics", {}).get("sessionStructure")
            if structure is None:
                raise CommandError("target session structure is missing")
            validate_opening_cohorts(addon, structure)
            _source_guard(snapshot, addon)
            current = _record(snapshot)
            before = current
            if canonical_sha256(snapshot.payload) != expected_sha:
                if not backup_path.exists():
                    raise CommandError("published payload changed since preflight")
                backup = _read_json(backup_path)
                before = backup["snapshot"]
                if backup.get("summarySha256") != summary_sha or canonical_sha256(before["payload"]) != expected_sha:
                    raise CommandError("recovery backup does not match import")
            after = copy.deepcopy(before)
            after["payload"].setdefault("metrics", {})["openingCohorts"] = addon
            after_sha = canonical_sha256(after["payload"])
            already_applied = current == after
            if current != before and not already_applied:
                raise CommandError("published payload or metadata changed since preflight")
            backup = {
                "schemaVersion": "teeni-opening-cohorts-backup/1.0.0",
                "summarySha256": summary_sha, "payloadSha256": expected_sha, "snapshot": before,
            }
            if backup_path.exists() and _read_json(backup_path) != backup:
                raise CommandError("existing backup differs; choose a new backup directory")
            receipt = {
                "schemaVersion": "teeni-opening-cohorts-import/1.0.0",
                "status": "validated", "productVersion": product, "dataDate": str(day),
                "summarySha256": summary_sha, "beforePayloadSha256": expected_sha,
                "afterPayloadSha256": after_sha, "backupFile": backup_path.name,
                "backupSha256": canonical_sha256(backup), "source": addon["source"],
                "changedFields": ["payload.metrics.openingCohorts"],
            }
            if options["apply"]:
                if not backup_path.exists():
                    _write_json(backup_path, backup)
                # A pending journal survives a lost post-commit response. Recovery compares the full target record.
                receipt["status"] = "pending"
                _write_json(receipt_path, receipt)
                if not already_applied:
                    DailySnapshot.objects.filter(pk=snapshot.pk).update(payload=after["payload"])
                snapshot.refresh_from_db()
                if _record(snapshot) != after:
                    raise CommandError("post-update readback changed protected snapshot fields")
        if options["apply"]:
            receipt["status"] = "recovered" if already_applied else "published"
            _write_json(receipt_path, receipt)
        self.stdout.write(json.dumps(receipt, ensure_ascii=False))

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from dashboard.product_versions import PRODUCT_VERSION_CHOICES
from interest_engine.history import repair_history_metrics
from interests.models import InterestSegmentSnapshot, InterestSnapshot
from interests.product_versions import payload_product_version
from topics.storage import safe_job_path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def repair_samples(payload):
    for group in payload.get("groups", []):
        group["sampleStatus"] = "可描述" if group.get("eligibleUsers", 0) >= 30 else "样本不足"


def resolve_base_versions(record, payload):
    method = payload.setdefault("method", {})
    if method.get("baseCoreVersion") and method.get("baseRulesVersion"):
        return True
    value = record.job.runtime_state.get("base", {}).get("manifestPath")
    if not value:
        return False
    try:
        manifest = json.loads(safe_job_path(record.job_id, value).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    detail = next((item for item in manifest.get("outputs", []) if item.get("role") == "primary_detail"), {})
    if (detail.get("sha256") != record.source_sha256
            or str(manifest.get("sceneId", "488")) != record.job.scene_id
            or manifest.get("contractVersion") != method.get("baseContract")
            or not manifest.get("coreVersion") or not manifest.get("rulesVersion")):
        return False
    method.update(baseCoreVersion=manifest["coreVersion"], baseRulesVersion=manifest["rulesVersion"])
    return True


class Command(BaseCommand):
    help = "Repair interest aggregates from retained aggregate evidence; dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", required=True)
        parser.add_argument("--date")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--product-version", choices=[item[0] for item in PRODUCT_VERSION_CHOICES], default="M1")

    def handle(self, *args, **options):
        try:
            selected = date.fromisoformat(options["date"]) if options["date"] else None
        except ValueError as exc:
            raise CommandError("date must be YYYY-MM-DD") from exc
        product_version = options["product_version"]
        snapshots = list(InterestSnapshot.objects.select_related("job").filter(product_version=product_version).order_by("data_date"))
        source_guards = {record.pk: digest(record.payload) for record in snapshots}
        resolved = {}
        for record in snapshots:
            payload = deepcopy(record.payload)
            if record.job.product_version != product_version or payload_product_version(payload) != product_version:
                raise CommandError("stored snapshot product version mismatch")
            known = resolve_base_versions(record, payload)
            resolved[record.pk] = (payload, known)
        changes = []
        for record in snapshots:
            if selected and record.data_date != selected:
                continue
            payload, known = resolved[record.pk]
            repair_samples(payload.get("demographicInterest", {}))
            history = [item for item, available in resolved.values() if available] if known else []
            repair_history_metrics(payload, history)
            payload["aggregateCorrection"] = {
                "version": "interest-aggregate-repair/1.0.0",
                "originalWorkbookSha256": record.workbook_sha256,
                "originalArtifactUnchanged": True,
                "historyProvenanceAvailable": known,
            }
            if payload != record.payload:
                changes.append({"model": "snapshot", "id": record.pk, "date": str(record.data_date),
                                "before": record.payload, "after": payload,
                                "beforeSha256": digest(record.payload), "baseProvenanceAvailable": known})
        segments = InterestSegmentSnapshot.objects.select_related("job").filter(product_version=product_version).order_by("data_date", "dimension_set")
        if selected:
            segments = segments.filter(data_date=selected)
        for record in segments:
            payload = deepcopy(record.payload)
            if record.job.product_version != product_version or payload_product_version(payload) != product_version:
                raise CommandError("stored segment product version mismatch")
            repair_samples(payload)
            if payload != record.payload:
                changes.append({"model": "segment", "id": record.pk, "date": str(record.data_date),
                                "before": record.payload, "after": payload, "beforeSha256": digest(record.payload)})
        output = Path(options["output_dir"]).resolve()
        output.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        plan = output / f"interest-aggregate-repair-{product_version}-{stamp}-{uuid.uuid4().hex}.json"
        with plan.open("x", encoding="utf-8") as stream:
            json.dump({"schemaVersion": "teeni-interest-aggregate-repair/1.0.0", "productVersion": product_version, "sourceSnapshotGuards": source_guards,
                       "changes": changes}, stream, ensure_ascii=False, indent=2)
        if options["apply"]:
            with transaction.atomic():
                observed = {record.pk: digest(record.payload) for record in InterestSnapshot.objects.select_for_update().filter(product_version=product_version).order_by("pk")}
                if observed != source_guards:
                    raise CommandError("history source changed after review; no changes were applied")
                for change in changes:
                    model = InterestSnapshot if change["model"] == "snapshot" else InterestSegmentSnapshot
                    record = model.objects.select_for_update().get(pk=change["id"], product_version=product_version)
                    if digest(record.payload) != change["beforeSha256"]:
                        raise CommandError("source aggregate changed after review; no changes were applied")
                    record.payload = change["after"]
                    record.save(update_fields=["payload"])
        self.stdout.write(json.dumps({"applied": bool(options["apply"]), "productVersion": product_version, "changedRows": len(changes),
                                      "backupAndPlan": str(plan)}, ensure_ascii=False))

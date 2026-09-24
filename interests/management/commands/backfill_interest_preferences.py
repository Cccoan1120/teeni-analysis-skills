import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from interest_engine.batch import load_batch_manifest
from interest_engine.contracts import sha256_file
from dashboard.product_versions import PRODUCT_VERSION_CHOICES
from interests.models import InterestSnapshot
from interests.preferences import build_contribution, save_contribution
from topics.storage import safe_job_path


class Command(BaseCommand):
    help = "Build cumulative contributions from retained inputs and published classifications, without reanalysis."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", action="append", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--product-version", choices=[item[0] for item in PRODUCT_VERSION_CHOICES], default="M1")

    def handle(self, *args, **options):
        entries = {}
        for path in options["manifest"]:
            contract = load_batch_manifest(path)
            if contract.product_version != options["product_version"]:
                raise CommandError("manifest product version does not match selected backfill version")
            for entry in contract.entries:
                entries[(str(entry.data_date), entry.detail_sha256)] = entry
        prepared = []
        try:
            for snapshot in InterestSnapshot.objects.select_related("job").filter(product_version=options["product_version"]).order_by("data_date"):
                entry = entries.get((str(snapshot.data_date), snapshot.source_sha256))
                if entry is None:
                    raise CommandError("retained input missing for " + str(snapshot.data_date))
                state = snapshot.job.runtime_state
                detail = safe_job_path(snapshot.job_id, state["interest"]["detailPath"])
                registry = safe_job_path(snapshot.job_id, state["interestRegistry"]["registryPath"])
                manifest = json.loads(safe_job_path(snapshot.job_id, state["interest"]["manifestPath"]).read_text(encoding="utf-8"))
                # The daily safe classification must still match its published verification manifest.
                output = next((item for item in manifest["outputs"] if item["file"] == detail.name), None)
                if not output or output["sha256"] != sha256_file(detail):
                    raise CommandError("published safe detail hash mismatch")
                payload = build_contribution(entry.detail_path, detail, registry, snapshot.payload)
                prepared.append((snapshot, payload))
                self.stderr.write(json.dumps({"verifiedDate": str(snapshot.data_date), "users": len(payload["users"]), "hits": len(payload["hits"])}))
            if options["apply"]:
                with transaction.atomic():
                    for snapshot, payload in prepared:
                        current = InterestSnapshot.objects.select_for_update().get(product_version=snapshot.product_version, data_date=snapshot.data_date)
                        if current.job_id != snapshot.job_id or current.source_sha256 != snapshot.source_sha256:
                            raise CommandError("published snapshot changed during backfill")
                        save_contribution(snapshot.payload, payload)
        except (ValueError, OSError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({"ok": True, "productVersion": options["product_version"], "applied": options["apply"], "dates": [str(item.data_date) for item, _ in prepared]}))

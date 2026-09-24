import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from dashboard.models import DailySnapshot
from publisher.privacy import AggregatePrivacyError, assert_aggregate_only
from publisher.snapshot import SnapshotContractError, validate_snapshot_payload
from dashboard.product_versions import product_version_for_scene


class Command(BaseCommand):
    help = "Load aggregate-only seed snapshots into the dashboard database."

    def add_arguments(self, parser):
        parser.add_argument(
            "directory",
            nargs="?",
            default="dashboard/data/seed_snapshots",
            type=Path,
        )

    def handle(self, *args, **options):
        directory: Path = options["directory"].resolve()
        if not directory.is_dir():
            raise CommandError(f"snapshot directory not found: {directory}")
        files = sorted(directory.glob("*.json"))
        if not files:
            raise CommandError("no snapshot JSON files found")

        loaded = 0
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                validate_snapshot_payload(payload)
                assert_aggregate_only(payload)
            except (OSError, json.JSONDecodeError, SnapshotContractError, AggregatePrivacyError) as exc:
                raise CommandError(f"invalid snapshot {path.name}: {exc}") from exc
            DailySnapshot.objects.update_or_create(
                data_date=payload["dataDate"],
                product_version=(
                    payload.get("productVersion")
                    or product_version_for_scene(payload["sceneId"])
                ),
                defaults={
                    "scene_id": payload["sceneId"],
                    "core_version": payload["coreVersion"],
                    "rules_version": payload["rulesVersion"],
                    "contract_version": payload["contractVersion"],
                    "source_sha256": payload["sourceSha256"],
                    "workbook_sha256": payload["workbookSha256"],
                    "payload": payload,
                },
            )
            loaded += 1
        self.stdout.write(self.style.SUCCESS(f"Loaded {loaded} aggregate snapshots"))

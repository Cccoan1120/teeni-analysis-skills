from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from dashboard.insights import InsightValidationError, STRUCTURE_VERSION, generated_content
from dashboard.models import DailySnapshot, ManagementInsight, ManagementInsightRevision


CONTENT_FIELDS = ("title", "summary", "facts", "mechanisms", "associations", "hypotheses", "actions")


def revision_payload(revision):
    return {field: getattr(revision, field) for field in CONTENT_FIELDS} | {
        "metricSnapshot": revision.metric_snapshot,
        "sourceHashes": revision.source_hashes,
    }


class Command(BaseCommand):
    help = "Refresh untouched automatic draft revisions; preserve old revisions and dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        output = Path(options["output_dir"]).resolve()
        output.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = output / f"automatic-insight-refresh-{stamp}-{uuid.uuid4().hex}.json"
        changes = []
        unchanged = 0
        skipped = 0
        with transaction.atomic():
            insights = list(ManagementInsight.objects.select_for_update().filter(
                origin=ManagementInsight.Origin.AUTOMATIC,
                status=ManagementInsight.Status.DRAFT,
                created_by__isnull=True,
                published_revision__isnull=True,
                published_at__isnull=True,
            ).order_by("id"))
            for insight in insights:
                revisions = list(insight.revisions.select_for_update().order_by("revision"))
                if not revisions or revisions[-1].revision != insight.current_revision or any(row.created_by_id is not None for row in revisions):
                    skipped += 1
                    continue
                previous = revisions[-1]
                snapshots = DailySnapshot.objects.filter(product_version=insight.product_version,
                    data_date__range=(insight.start_date, insight.end_date))
                if any(row.payload.get('metrics', {}).get('sessionStructure', {}).get('schemaVersion') != STRUCTURE_VERSION for row in snapshots):
                    skipped += 1
                    continue
                try:
                    content, metrics, hashes = generated_content(
                        insight.start_date, insight.end_date, insight.product_version,
                    )
                except InsightValidationError as exc:
                    raise CommandError(f"cannot regenerate automatic insight {insight.pk}: {exc}") from exc
                after = content | {"metricSnapshot": metrics, "sourceHashes": hashes}
                before = revision_payload(previous)
                if before == after:
                    unchanged += 1
                    continue
                changes.append({
                    "insightId": str(insight.pk), "productVersion": insight.product_version,
                    "startDate": insight.start_date.isoformat(), "endDate": insight.end_date.isoformat(),
                    "previousRevision": previous.revision, "nextRevision": previous.revision + 1,
                    "previousUpdatedAt": insight.updated_at.isoformat(),
                    "previousRevisionCreatedAt": previous.created_at.isoformat(),
                    "before": before, "after": after,
                })
            with backup.open("x", encoding="utf-8") as stream:
                json.dump({"schemaVersion": "teeni-automatic-insight-refresh/1.0.0", "changes": changes},
                          stream, ensure_ascii=False, indent=2)
            if options["apply"]:
                for change in changes:
                    after = change["after"]
                    ManagementInsightRevision.objects.create(
                        insight_id=change["insightId"], revision=change["nextRevision"], created_by=None,
                        metric_snapshot=after["metricSnapshot"], source_hashes=after["sourceHashes"],
                        **{field: after[field] for field in CONTENT_FIELDS},
                    )
                    insight = next(item for item in insights if str(item.pk) == change["insightId"])
                    insight.current_revision = change["nextRevision"]
                    insight.save(update_fields=["current_revision", "updated_at"])
        self.stdout.write(json.dumps({
            "applied": bool(options["apply"]), "considered": len(insights),
            "wouldRefresh": len(changes), "refreshed": len(changes) if options["apply"] else 0,
            "unchanged": unchanged, "skipped": skipped, "backupAndPlan": str(backup),
        }, ensure_ascii=False))

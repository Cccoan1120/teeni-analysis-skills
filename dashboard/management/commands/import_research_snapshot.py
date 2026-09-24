import json
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from dashboard.models import DailySnapshot, ProductResearchSnapshot
from dashboard.research import validate_report
from interest_engine.contracts import sha256_file
from publisher.privacy import assert_aggregate_only


class Command(BaseCommand):
    help = "Import a local aggregate research report; private review files are never imported."

    def add_arguments(self, parser):
        parser.add_argument("--summary", required=True)
        parser.add_argument("--sha256", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["summary"])
        if sha256_file(path) != options["sha256"]:
            raise CommandError("summary hash mismatch")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        try:
            validate_report(payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        day = date.fromisoformat(payload["dataDate"])
        with transaction.atomic():
            base = DailySnapshot.objects.select_for_update().filter(product_version=payload["productVersion"], data_date=day).first()
            source = next((row for row in payload["sources"] if row["dataDate"] == str(day)), None)
            if not base or not source or base.payload.get("baseDetailSha256") != source["detailSha256"]:
                raise CommandError("research inputs do not match published base detail")
            for observed in payload['sources']:
                observed_base = DailySnapshot.objects.select_for_update().filter(product_version=payload['productVersion'], data_date=observed['dataDate']).first()
                if not observed_base or observed_base.payload.get('baseDetailSha256') != observed['detailSha256']:
                    raise CommandError('research observation window does not match published base detail')
            historical = payload.get('evaluationProvenance')
            if historical:
                previous = ProductResearchSnapshot.objects.select_for_update().filter(product_version=payload['productVersion'], data_date=day).first()
                if not previous:
                    raise CommandError('historical evaluation requires its previously published report')
                original = previous.payload.get('evaluationProvenance')
                if original:
                    if historical != original:
                        raise CommandError('historical evaluation provenance changed')
                elif historical['sourceReportSha256'] != previous.source_sha256 or historical['sources'] != previous.payload['sources']:
                    raise CommandError('historical evaluation must bind the previously published source')
                for key in ('evaluation', 'aiEvaluation'):
                    if payload.get(key) != previous.payload.get(key):
                        raise CommandError('historical evaluation contents changed')
            if options["apply"]:
                ProductResearchSnapshot.objects.update_or_create(product_version=payload["productVersion"], data_date=day,
                    defaults={"payload": payload, "source_sha256": options["sha256"]})
        self.stdout.write(json.dumps({"status": "published" if options["apply"] else "validated", "dataDate": str(day), "productVersion": payload["productVersion"]}))

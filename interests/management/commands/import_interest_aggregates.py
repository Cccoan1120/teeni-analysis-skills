"""Publish locally verified aggregates without transferring original conversations."""
import hashlib
import hmac
import json
import gzip
import re
import shutil
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from dashboard.models import DailySnapshot
from interest_engine.contracts import sha256_file
from interest_engine.segments import validate_segment_payload
from interests.models import InterestDailyContribution, InterestSnapshot, InterestSegmentSnapshot
from interests.preferences import SCHEMA, _identity, _secret, _metrics, save_contribution
from interests.segment_store import replace_segment_snapshots, stored_segment_payload
from publisher.interest_snapshot import validate_interest_payload
from publisher.privacy import assert_aggregate_only
from topics.models import TopicJob, TopicArtifact
from topics.storage import job_directory


class Command(BaseCommand):
    help = "Validate or publish an authenticated local aggregate bundle."

    def add_arguments(self, parser):
        parser.add_argument("--package", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--backup-dir")

    def handle(self, *args, **options):
        manifest_path = Path(options["package"]).resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        signature = manifest.pop("signature", "")
        digest = hmac.new(_secret(), json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, digest) or manifest.get("identitySha256") != _identity():
            raise CommandError("publication authentication failed")
        if manifest.get("schemaVersion") != "teeni-interest-aggregate-publication/1.0.0" or manifest.get("candidateReviewComplete") is not True:
            raise CommandError("verified reviewed publication required")
        required = {"snapshot", "segments", "contribution", "workbook"}
        if set(manifest["artifacts"]) != required:
            raise CommandError("unexpected aggregate artifacts")
        paths = {}
        for role, item in manifest["artifacts"].items():
            if Path(item["file"]).name != item["file"]:
                raise CommandError("unsafe artifact path")
            paths[role] = manifest_path.parent / item["file"]
            if sha256_file(paths[role]) != item["sha256"]:
                raise CommandError("artifact hash mismatch")
        snapshot, segments, contribution = [json.loads(paths[role].read_text(encoding="utf-8")) for role in ("snapshot", "segments", "contribution")]
        product, day = snapshot["productVersion"], date.fromisoformat(snapshot["dataDate"])
        if product != manifest["productVersion"] or str(day) != manifest["dataDate"]:
            raise CommandError("publication product/date mismatch")
        actor = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("date_joined").first()
        if actor is None:
            raise CommandError("active publishing administrator required")
        job = TopicJob(created_by=actor, product_version=product, data_date=day, original_name="verified-aggregate-publication",
                       expected_size=0, received_size=0, expected_sha256=snapshot["sourceSha256"], input_sha256=snapshot["sourceSha256"],
                       status=TopicJob.Status.COMPLETED, progress=100, started_at=timezone.now(), finished_at=timezone.now())
        validate_interest_payload(snapshot, job)
        assert_aggregate_only(segments)
        validate_segment_payload(segments, data_date=str(day), source_sha256=snapshot["sourceSha256"], registry_sha256=snapshot["registrySha256"], product_version=product, scene_id=snapshot["sceneId"])
        if contribution.get("productVersion") != product or contribution.get("date") != str(day):
            raise CommandError("contribution product/date mismatch")
        expected_compatibility = {"engineVersion": snapshot["engineVersion"], "detailSchema": snapshot["detailSchema"],
                                  "baseCoreVersion": snapshot["method"].get("baseCoreVersion"), "baseRulesVersion": snapshot["method"].get("baseRulesVersion")}
        if (set(contribution) != {"schemaVersion", "date", "productVersion", "sceneId", "users", "compatibility", "hits", "ips", "regions", "rows", "detailSha256"}
                or contribution["schemaVersion"] != SCHEMA or contribution["sceneId"] != snapshot["sceneId"]
                or contribution["compatibility"] != expected_compatibility or contribution["rows"] != snapshot["totals"]["inputQueries"]
                or not re.fullmatch('[0-9a-f]{64}', contribution['detailSha256'])):
            raise CommandError('invalid contribution contract')
        if set(contribution['ips']) != {row['id'] for row in snapshot['ipRollups']}:
            raise CommandError('contribution IP set mismatch')
        for user, profile in contribution['users'].items():
            if not re.fullmatch('[0-9a-f]{64}', user) or not isinstance(profile, list) or len(profile) != 4 or type(profile[3]) is not bool:
                raise CommandError('invalid private account contribution')
        hit_keys = set()
        for hit in contribution['hits']:
            if (len(hit) != 7 or hit[0] not in contribution['users'] or hit[1] not in contribution['ips']
                    or not re.fullmatch('[0-9a-f]{64}', hit[2]) or type(hit[3]) is not int or hit[3] <= 0
                    or any(type(value) is not bool for value in hit[4:6])
                    or not hit[6] or not set(hit[6]) <= {'positive', 'negative', 'neutral', 'mixed'}
                    or tuple(hit[:3]) in hit_keys):
                raise CommandError('invalid private hit contribution')
            hit_keys.add(tuple(hit[:3]))
        metrics = {row["id"]: row for row in _metrics(contribution["hits"], set(contribution["users"]), contribution["ips"])}
        for row in snapshot["ipRollups"]:
            actual = metrics.get(row["id"], {})
            for field in ("activeInterestUsers", "mentionUsers", "initiatorUsers", "continuationUsers", "queries", "sessions", "deepSessions", "positivePreferenceUsers", "negativePreferenceUsers", "neutralMentionUsers", "mixedPreferenceUsers"):
                if actual.get(field) != row.get(field):
                    raise CommandError("contribution aggregate reconciliation failed")
        with transaction.atomic():
            base = DailySnapshot.objects.select_for_update().filter(product_version=product, data_date=day).first()
            if not base or base.payload.get("baseDetailSha256") != snapshot["sourceSha256"]:
                raise CommandError("interest source differs from published base detail")
            existing = InterestSnapshot.objects.filter(product_version=product, data_date=day).first()
            stored = InterestDailyContribution.objects.filter(product_version=product, data_date=day, identity_sha256=_identity()).first()
            unchanged = bool(existing and existing.payload == snapshot and existing.workbook_sha256 == manifest['artifacts']['workbook']['sha256']
                             and stored and json.loads(gzip.decompress(bytes(stored.payload))) == contribution)
            stored_segments = {row.dimension_set: row.payload for row in InterestSegmentSnapshot.objects.filter(product_version=product, data_date=day)}
            unchanged = unchanged and stored_segments == {key: stored_segment_payload(segments, key) for key in segments['dimensionSets']}
            if unchanged:
                artifact = existing.job.artifacts.filter(kind=TopicArtifact.Kind.INTEREST_WORKBOOK, verified=True, expires_at__gt=timezone.now()).first()
                unchanged = bool(artifact and (job_directory(existing.job_id) / artifact.relative_path).is_file()
                                 and sha256_file(job_directory(existing.job_id) / artifact.relative_path) == artifact.sha256)
            if unchanged:
                self.stdout.write(json.dumps({'status': 'unchanged', 'productVersion': product, 'dataDate': str(day), 'sourceSha256': snapshot['sourceSha256']}))
                return
            if options["apply"]:
                if not options["backup_dir"]:
                    raise CommandError("scoped backup directory required")
                backup = Path(options["backup_dir"]).resolve()
                backup.mkdir(parents=True, exist_ok=False, mode=0o700)
                for name, model in (("snapshots", InterestSnapshot), ("segments", InterestSegmentSnapshot), ("contributions", InterestDailyContribution)):
                    (backup / f"{name}.json").write_text(serializers.serialize("json", model.objects.filter(product_version=product, data_date=day)), encoding="utf-8")
                root = job_directory(job.id, create=True)
                target = root / "interest-report.xlsx"
                shutil.copy2(paths["workbook"], target)
                job.runtime_state = {"interest": {"verified": True, "candidateReviewComplete": True, "sourceMode": "local_aggregate_bundle"}}
                job.save()
                TopicArtifact.objects.create(job=job, kind=TopicArtifact.Kind.INTEREST_WORKBOOK, relative_path="interest-report.xlsx",
                    download_name=f"Teeni-{product}-{day}-interest.xlsx", sha256=sha256_file(target), byte_size=target.stat().st_size, verified=True)
                InterestSnapshot.objects.update_or_create(product_version=product, data_date=day, defaults={"job": job,
                    "schema_version": snapshot["schemaVersion"], "engine_version": snapshot["engineVersion"], "source_sha256": snapshot["sourceSha256"],
                    "registry_sha256": snapshot["registrySha256"], "workbook_sha256": manifest["artifacts"]["workbook"]["sha256"], "payload": snapshot})
                replace_segment_snapshots(job, segments)
                save_contribution(snapshot, contribution)
        self.stdout.write(json.dumps({"status": "published" if options["apply"] else "validated", "dataDate": str(day), "productVersion": product,
                                      "sourceSha256": snapshot["sourceSha256"], "replaced": existing is not None}))

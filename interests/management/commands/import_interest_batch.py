from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from interest_engine.batch import BatchContractError, analyze_batch, load_batch_manifest, load_batch_result
from interest_engine.contracts import sha256_file
from interest_engine.model import LocalCandidatePassthroughEnricher, QwenCandidateEnricher
from interest_engine.segments import DEFAULT_PROVINCE_MAP, ProvinceMap, validate_segment_payload
from interests.models import InterestRegistrySnapshot, InterestSnapshot
from interests.registry import ensure_registry
from interests.segment_store import replace_segment_snapshots
from interests.preferences import build_contribution, save_contribution, _identity
from interests.models import InterestDailyContribution
from publisher.interest_snapshot import validate_interest_payload
from topics.models import TopicArtifact, TopicJob
from topics.storage import job_directory, safe_job_path, storage_root


def _candidate_enricher(candidate_review_complete: bool):
    if candidate_review_complete:
        return LocalCandidatePassthroughEnricher()
    if os.environ.get("TEENI_INTEREST_CANDIDATE_MODEL") != "1":
        return None
    if not (os.environ.get("TEENI_TOPICS_API_KEY") or os.environ.get("TEENI_TOPICS_API_KEY_FILE")):
        return None
    return QwenCandidateEnricher.from_environment()


def _copy(path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(path, destination)


def _publication_matches(existing, payload: dict) -> bool:
    if not existing or existing.payload != payload or existing.job.status != TopicJob.Status.COMPLETED:
        return False
    required = {TopicArtifact.Kind.INTEREST_WORKBOOK, TopicArtifact.Kind.INTEREST_DETAIL_ZIP}
    artifacts = list(existing.job.artifacts.filter(kind__in=required, verified=True, expires_at__gt=timezone.now()))
    if {item.kind for item in artifacts} != required:
        return False
    try:
        return all(sha256_file(safe_job_path(existing.job_id, item.relative_path)) == item.sha256 for item in artifacts)
    except OSError:
        return False


class Command(BaseCommand):
    help = "Analyze 1-7 verified interest packages or publish their verified result without reanalysis."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True)
        parser.add_argument("--publish", action="store_true")
        parser.add_argument("--result-manifest")
        parser.add_argument("--cache-dir")
        parser.add_argument("--backup-dir")

    def handle(self, *args, **options):
        try:
            contract = load_batch_manifest(options["manifest"])
        except BatchContractError as exc:
            raise CommandError(str(exc)) from exc
        if options["publish"] and not contract.candidate_review_complete:
            raise CommandError("candidateReviewComplete must be true before production publication")

        batch_key = sha256_file(contract.manifest_path)[:16]
        output_root = Path(settings.TEENI_TOPIC_STORAGE_ROOT).resolve() / "interest-batches" / batch_key
        def progress(stage, **values):
            self.stderr.write(json.dumps({"stage": stage, **values}, ensure_ascii=True))
            self.stderr.flush()
        try:
            if options.get("result_manifest"):
                artifacts = load_batch_result(options["result_manifest"], contract, progress=progress)
            else:
                artifacts = analyze_batch(
                    contract, output_root,
                    enricher=_candidate_enricher(contract.candidate_review_complete),
                    cache_dir=options.get("cache_dir") or output_root.parent.parent / "interest-daily-cache",
                    progress=progress,
                )
        except (BatchContractError, ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        if not options["publish"]:
            self.stdout.write(json.dumps({
                "ok": True,
                "published": False,
                "dates": [entry.data_date.isoformat() for entry in contract.entries],
                "productVersion": contract.product_version, "sceneId": contract.scene_id,
                "candidateDegraded": artifacts.candidate_degraded,
                "resultManifest": str(artifacts.manifest_path),
            }, ensure_ascii=True))
            return

        actor = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("date_joined").first()
        if actor is None:
            raise CommandError("an active superuser is required for batch publication")

        if options.get("backup_dir"):
            from django.core import serializers
            from interests.models import InterestAlias, InterestEntity, InterestSegmentSnapshot
            backup = Path(options["backup_dir"]).resolve()
            backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            dates = [entry.data_date for entry in contract.entries]
            for name, queryset in (
                ("interest-snapshots-before.json", InterestSnapshot.objects.filter(product_version=contract.product_version, data_date__in=dates)),
                ("interest-segments-before.json", InterestSegmentSnapshot.objects.filter(product_version=contract.product_version, data_date__in=dates)),
                ("interest-contributions-before.json", InterestDailyContribution.objects.filter(product_version=contract.product_version, data_date__in=dates)),
                ("interest-registry-entities-before.json", InterestEntity.objects.all()),
                ("interest-registry-aliases-before.json", InterestAlias.objects.all()),
            ):
                with (backup / name).open("x", encoding="utf-8") as stream:
                    serializers.serialize("json", queryset, stream=stream)
            (backup / "scoped-backup-receipt.json").write_text(json.dumps({
                "dates": [item.isoformat() for item in dates],
                "productVersion": contract.product_version, "sceneId": contract.scene_id,
                "files": [{"file": path.name, "sha256": sha256_file(path)}
                          for path in sorted(backup.glob("*-before.json"))],
            }, ensure_ascii=True), encoding="utf-8")

        prepared = []
        contributions = []
        created_directories: list[Path] = []
        province_map = ProvinceMap.load(DEFAULT_PROVINCE_MAP)
        try:
            for entry, artifact in zip(contract.entries, artifacts.daily):
                existing_contribution = InterestDailyContribution.objects.filter(
                    product_version=contract.product_version, data_date=entry.data_date, source_sha256=entry.detail_sha256,
                    registry_sha256=contract.registry_sha256, identity_sha256=_identity(),
                    detail_sha256=sha256_file(artifact.detail_path),
                ).exists()
                if not existing_contribution:
                    contribution = build_contribution(entry.detail_path, artifact.detail_path, contract.registry_path, artifact.snapshot)
                    contributions.append((artifact.snapshot, contribution))
                existing = InterestSnapshot.objects.select_related("job").filter(product_version=contract.product_version, data_date=entry.data_date).first()
                if _publication_matches(existing, artifact.snapshot):
                    progress("publication_unchanged", dataDate=entry.data_date.isoformat())
                    continue
                job = TopicJob(
                    created_by=actor,
                    product_version=contract.product_version,
                    data_date=entry.data_date,
                    original_name=entry.detail_path.name,
                    pipeline=TopicJob.Pipeline.INTEREST_V1,
                    expected_size=entry.detail_path.stat().st_size,
                    received_size=entry.detail_path.stat().st_size,
                    expected_sha256=entry.detail_sha256,
                    input_sha256=entry.detail_sha256,
                    status=TopicJob.Status.COMPLETED,
                    progress=100,
                    started_at=timezone.now(),
                    finished_at=timezone.now(),
                )
                root = job_directory(job.id, create=True).resolve()
                created_directories.append(root)
                copied = {}
                for key, source, relative in (
                    ("workbook", artifact.workbook_path, "interest/teeni-interest-report.xlsx"),
                    ("detail", artifact.detail_path, "interest/teeni-interest-detail.safe.csv"),
                    ("privateDetail", artifact.private_detail_path, "interest/teeni-interest-detail.csv"),
                    ("snapshot", artifact.snapshot_path, "interest/teeni-interest-snapshot.json"),
                    ("segments", artifact.segment_path, "interest/teeni-interest-segments.json"),
                    ("candidate", artifact.candidate_path, "interest/teeni-interest-candidates.private.json"),
                    ("manifest", artifact.manifest_path, "interest/teeni-interest-manifest.json"),
                    ("registry", contract.registry_path, "runtime/interest-1.4.0/entity-registry.json"),
                ):
                    destination = safe_job_path(job.id, relative, create_parent=True)
                    _copy(source, destination)
                    copied[key] = destination

                archive_name = f"Teeni{contract.product_version}兴趣安全明细_{entry.data_date:%Y%m%d}.zip"
                archive = safe_job_path(job.id, f"artifacts/{archive_name}", create_parent=True)
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
                    bundle.write(copied["detail"], arcname=f"Teeni{contract.product_version}兴趣安全明细_{entry.data_date:%Y%m%d}.csv")
                payload = json.loads(copied["snapshot"].read_text(encoding="utf-8"))
                validate_interest_payload(payload, job)
                segments = json.loads(copied["segments"].read_text(encoding="utf-8"))
                validate_segment_payload(
                    segments,
                    data_date=entry.data_date.isoformat(),
                    source_sha256=entry.detail_sha256,
                    registry_sha256=contract.registry_sha256,
                    province_map_sha256=province_map.sha256,
                    rollup_ids=[row["id"] for row in payload["ipRollups"]],
                )
                relative = lambda path: path.resolve().relative_to(root).as_posix()
                job.runtime_state = {
                    "interestRegistry": {
                        "registryPath": relative(copied["registry"]),
                        "registryVersion": "batch-final",
                        "registrySha256": contract.registry_sha256,
                    },
                    "interest": {
                        "workbookPath": relative(copied["workbook"]),
                        "detailPath": relative(copied["detail"]),
                        "privateDetailPath": relative(copied["privateDetail"]),
                        "snapshotPath": relative(copied["snapshot"]),
                        "segmentPath": relative(copied["segments"]),
                        "candidatePath": relative(copied["candidate"]),
                        "manifestPath": relative(copied["manifest"]),
                    },
                    "interestBatch": {
                        "sourceManifestSha256": sha256_file(contract.manifest_path),
                        "resultManifestSha256": sha256_file(artifacts.manifest_path),
                    },
                }
                prepared.append((job, payload, segments, copied, archive, archive_name))

            with transaction.atomic():
                # Serialize batch publishers, including first publication of a date.
                get_user_model().objects.select_for_update().get(pk=actor.pk)
                ensure_registry(contract.registry_path)
                for contribution_snapshot, contribution in contributions:
                    save_contribution(contribution_snapshot, contribution)
                for job, payload, segments, copied, archive, archive_name in prepared:
                    existing = InterestSnapshot.objects.filter(product_version=job.product_version, data_date=job.data_date).first()
                    if _publication_matches(existing, payload):
                        shutil.rmtree(job_directory(job.id))
                        continue
                    job.save(force_insert=True)
                    InterestRegistrySnapshot.objects.create(
                        job=job,
                        registry_version="batch-final",
                        sha256=contract.registry_sha256,
                        relative_path=job.runtime_state["interestRegistry"]["registryPath"],
                    )
                    InterestSnapshot.objects.update_or_create(
                        product_version=job.product_version,
                        data_date=job.data_date,
                        defaults={
                            "job": job,
                            "schema_version": payload["schemaVersion"],
                            "engine_version": payload["engineVersion"],
                            "source_sha256": payload["sourceSha256"],
                            "registry_sha256": payload["registrySha256"],
                            "workbook_sha256": sha256_file(copied["workbook"]),
                            "payload": payload,
                        },
                    )
                    replace_segment_snapshots(job, segments)
                    progress("published_date", dataDate=job.data_date.isoformat())
                    root = job_directory(job.id).resolve()
                    for kind, path, download_name in (
                        (TopicArtifact.Kind.INTEREST_WORKBOOK, copied["workbook"], f"Teeni_{job.product_version}_IP兴趣分析_{job.data_date:%Y%m%d}.xlsx"),
                        (TopicArtifact.Kind.INTEREST_DETAIL_ZIP, archive, archive_name),
                    ):
                        TopicArtifact.objects.create(
                            job=job,
                            kind=kind,
                            relative_path=path.resolve().relative_to(root).as_posix(),
                            download_name=download_name,
                            sha256=sha256_file(path),
                            byte_size=path.stat().st_size,
                            verified=True,
                            expires_at=job.expires_at,
                        )
        except Exception:
            jobs_root = (storage_root() / "jobs").resolve()
            for directory in created_directories:
                if directory.parent == jobs_root and directory.is_dir():
                    shutil.rmtree(directory)
            raise

        self.stdout.write(json.dumps({
            "ok": True,
            "published": True,
            "dates": [entry.data_date.isoformat() for entry in contract.entries],
                "productVersion": contract.product_version, "sceneId": contract.scene_id,
            "registrySha256": contract.registry_sha256,
            "candidateDegraded": artifacts.candidate_degraded,
            "resultManifest": str(artifacts.manifest_path),
        }, ensure_ascii=True))

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from interest_engine.batch import BATCH_RESULT_SCHEMA, load_batch_manifest
from interest_engine.contracts import sha256_file
from interests.product_versions import payload_product_version
from interests.models import InterestCandidate
from interests.registry import freeze_registry
from topics.models import TopicJob
from topics.storage import job_directory, safe_job_path


def _load_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CommandError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise CommandError(f"{label} must be a JSON object")
    return payload


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class Command(BaseCommand):
    help = "Freeze a fully reviewed staged registry and create a review-complete batch manifest."

    def add_arguments(self, parser):
        parser.add_argument("--job", required=True)
        parser.add_argument("--manifest", required=True)
        parser.add_argument("--output-manifest", required=True)

    def handle(self, *args, **options):
        try:
            job = TopicJob.objects.get(id=options["job"], pipeline=TopicJob.Pipeline.INTEREST_V1)
        except (TopicJob.DoesNotExist, ValueError) as exc:
            raise CommandError("staged interest candidate job does not exist") from exc
        staging = job.runtime_state.get("interestCandidateStaging")
        if not isinstance(staging, dict):
            raise CommandError("job is not a staged interest candidate review")
        candidate_count = InterestCandidate.objects.filter(job=job).count()
        if candidate_count == 0:
            raise CommandError("staged candidate job is empty")
        pending = InterestCandidate.objects.filter(job=job, status=InterestCandidate.Status.PENDING).count()
        if pending:
            raise CommandError(f"candidate review is incomplete: {pending} pending")

        source_manifest = Path(options["manifest"]).resolve()
        output_manifest = Path(options["output_manifest"]).resolve()
        if output_manifest.parent != source_manifest.parent:
            raise CommandError("reviewed batch manifest must stay beside the source manifest")
        source_contract = load_batch_manifest(source_manifest)
        if source_contract.product_version != job.product_version or payload_product_version(staging) != job.product_version:
            raise CommandError("source batch product version does not match staged review")
        expected_dates = [entry.data_date.isoformat() for entry in source_contract.entries]
        if expected_dates != staging.get("dates"):
            raise CommandError("source batch dates do not match the staged review")
        if source_contract.registry_sha256 != staging.get("registrySha256"):
            raise CommandError("source batch registry does not match the staged review")

        result_relative = str(staging.get("resultPath") or "")
        result_path = safe_job_path(job.id, result_relative)
        result = _load_json(result_path, "staged result manifest")
        if payload_product_version(result) != job.product_version:
            raise CommandError("staged result product version mismatch")
        if result.get("schemaVersion") != BATCH_RESULT_SCHEMA:
            raise CommandError("staged result manifest schema is invalid")
        if result.get("sourceManifestSha256") != sha256_file(source_manifest):
            raise CommandError("source batch manifest does not match the staged result")

        frozen = freeze_registry(job)
        frozen_path = safe_job_path(job.id, frozen.relative_path)
        if sha256_file(frozen_path) != frozen.sha256:
            raise CommandError("frozen reviewed registry SHA-256 mismatch")

        output_registry = output_manifest.with_name(f"teeni-interest-registry.{job.product_version}.reviewed.json")
        output_registry.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(frozen_path, output_registry)
        reviewed_registry_sha256 = sha256_file(output_registry)

        payload = _load_json(source_manifest, "source batch manifest")
        payload["registryPath"] = output_registry.name
        payload["registrySha256"] = reviewed_registry_sha256
        payload["candidateReviewComplete"] = True
        _write_json_atomic(output_manifest, payload)
        reviewed_contract = load_batch_manifest(output_manifest)
        if not reviewed_contract.candidate_review_complete:
            raise CommandError("reviewed batch manifest did not preserve the review gate")

        runtime_state = dict(job.runtime_state)
        staging_state = dict(staging)
        staging_state.update({
            "candidateReviewComplete": True,
            "reviewedAt": timezone.now().isoformat(),
            "reviewedRegistrySha256": reviewed_registry_sha256,
            "reviewedManifestSha256": sha256_file(output_manifest),
        })
        runtime_state["interestCandidateStaging"] = staging_state
        job.runtime_state = runtime_state
        job.status = TopicJob.Status.FINAL_VERIFYING
        job.progress = 97
        job.save(update_fields=["runtime_state", "status", "progress", "updated_at"])

        self.stdout.write(json.dumps({
            "ok": True,
            "jobId": str(job.id),
            "reviewedCandidates": candidate_count,
            "pendingCandidates": 0,
            "registryEntities": len(_load_json(output_registry, "reviewed registry").get("entities", [])),
            "registrySha256": reviewed_registry_sha256,
            "manifestSha256": sha256_file(output_manifest),
            "outputManifest": str(output_manifest),
        }, ensure_ascii=True))

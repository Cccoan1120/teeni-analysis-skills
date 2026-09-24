from __future__ import annotations

import json
import math
import re
import shutil
from datetime import date, timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from interest_engine.batch import BATCH_CANDIDATE_SCHEMA, BATCH_RESULT_SCHEMA
from interest_engine.contracts import sha256_file
from interests.product_versions import payload_product_version
from interests.models import InterestCandidate, InterestSnapshot
from interests.registry import SEED_PATH, ensure_seed_registry
from topics.models import TopicArtifact, TopicJob
from topics.storage import job_directory, safe_job_path, storage_root


HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


def _load_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CommandError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise CommandError(f"{label} must be a JSON object")
    return payload


def _resolve(base: Path, value: object, label: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise CommandError(f"{label} is required")
    path = Path(raw)
    path = (base / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_file():
        raise CommandError(f"{label} does not exist")
    return path


def _verify_hash(path: Path, expected: object, label: str) -> str:
    digest = str(expected or "")
    if not HASH_PATTERN.fullmatch(digest) or sha256_file(path) != digest:
        raise CommandError(f"{label} SHA-256 mismatch")
    return digest


def _required_text(row: dict, key: str, maximum: int) -> str:
    value = str(row.get(key) or "").strip()
    if not value or len(value) > maximum:
        raise CommandError(f"candidate {key} must contain 1 to {maximum} characters")
    return value


def _optional_text(row: dict, key: str, maximum: int) -> str:
    value = str(row.get(key) or "").strip()
    if len(value) > maximum:
        raise CommandError(f"candidate {key} exceeds {maximum} characters")
    return value


def _count(row: dict, key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CommandError(f"candidate {key} must be a non-negative integer")
    return value


def _growth(row: dict) -> float | None:
    value = row.get("sevenDayGrowth")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CommandError("candidate sevenDayGrowth must be finite or null")
    return float(value)


def _candidate_rows(payload: dict) -> list[dict]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > 200:
        raise CommandError("candidate package must contain at most 200 candidates")
    rows = []
    normalized_values = set()
    for source in candidates:
        if not isinstance(source, dict):
            raise CommandError("candidate entries must be objects")
        normalized = _required_text(source, "normalizedPhrase", 80)
        if normalized in normalized_values:
            raise CommandError("candidate normalizedPhrase values must be unique")
        normalized_values.add(normalized)
        aliases = source.get("suggestedAliases", [])
        if not isinstance(aliases, list) or len(aliases) > 10:
            raise CommandError("candidate suggestedAliases must contain at most 10 values")
        clean_aliases = []
        for value in aliases:
            alias = str(value).strip()
            if not alias or len(alias) > 80:
                raise CommandError("candidate aliases must contain 1 to 80 characters")
            clean_aliases.append(alias)
        rows.append({
            "phrase": _required_text(source, "phrase", 80),
            "normalized_phrase": normalized,
            "query_count": _count(source, "queryCount"),
            "users": _count(source, "users"),
            "sessions": _count(source, "sessions"),
            "seven_day_growth": _growth(source),
            "suggested_name": _optional_text(source, "suggestedName", 80),
            "suggested_type": _optional_text(source, "suggestedType", 32),
            "suggested_subtype": _optional_text(source, "suggestedSubtype", 32),
            "suggested_parent_name": _optional_text(source, "suggestedParentName", 80),
            "suggested_aliases": clean_aliases,
            "model_confidence": _optional_text(source, "modelConfidence", 16),
            "model_recommended": source.get("accepted") is True,
        })
    return rows


def _result_contract(result_path: Path) -> tuple[dict, Path, list[dict], tuple[date, ...]]:
    result = _load_json(result_path, "result manifest")
    if result.get("schemaVersion") != BATCH_RESULT_SCHEMA:
        raise CommandError("unsupported interest batch result schema")
    try:
        product_version = payload_product_version(result)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    raw_dates = result.get("dates")
    if not isinstance(raw_dates, list) or not 1 <= len(raw_dates) <= 7:
        raise CommandError("result manifest must contain one to seven dates")
    try:
        dates = tuple(date.fromisoformat(str(value)) for value in raw_dates)
    except ValueError as exc:
        raise CommandError("result manifest contains an invalid date") from exc
    if len(set(dates)) != len(dates) or any(current - previous != timedelta(days=1) for previous, current in zip(dates, dates[1:])):
        raise CommandError("result manifest dates must be unique and contiguous")

    registry_sha256 = str(result.get("registrySha256") or "")
    if not HASH_PATTERN.fullmatch(registry_sha256):
        raise CommandError("result manifest registrySha256 is invalid")
    if sha256_file(SEED_PATH) != registry_sha256:
        raise CommandError("result registry does not match the local review registry")

    daily = result.get("daily")
    if not isinstance(daily, list) or len(daily) != len(dates):
        raise CommandError("result manifest daily output count must match dates")
    for expected_date, item in zip(dates, daily):
        if not isinstance(item, dict) or item.get("dataDate") != expected_date.isoformat():
            raise CommandError("daily output dates do not match the result date range")
        for path_key, hash_key, label in (
            ("snapshotPath", "snapshotSha256", "daily snapshot"),
            ("manifestPath", "manifestSha256", "daily manifest"),
        ):
            path = _resolve(result_path.parent, item.get(path_key), label)
            _verify_hash(path, item.get(hash_key), label)
            if payload_product_version(_load_json(path, label)) != product_version:
                raise CommandError("daily output product version mismatch")

    candidate_path = _resolve(result_path.parent, result.get("candidatePath"), "candidate package")
    _verify_hash(candidate_path, result.get("candidateSha256"), "candidate package")
    candidate_payload = _load_json(candidate_path, "candidate package")
    if payload_product_version(candidate_payload) != product_version:
        raise CommandError("candidate package product version mismatch")
    if candidate_payload.get("schemaVersion") != BATCH_CANDIDATE_SCHEMA:
        raise CommandError("unsupported interest candidate package schema")
    if candidate_payload.get("registrySha256") != registry_sha256:
        raise CommandError("candidate package registry SHA-256 mismatch")
    return result, candidate_path, _candidate_rows(candidate_payload), dates


class Command(BaseCommand):
    help = "Stage a verified 1-7 day interest candidate package for staff review without publishing snapshots."

    def add_arguments(self, parser):
        parser.add_argument("--result", required=True)

    def handle(self, *args, **options):
        result_path = Path(options["result"]).resolve()
        if not result_path.is_file():
            raise CommandError("result manifest does not exist")
        result, candidate_path, rows, dates = _result_contract(result_path)
        product_version = payload_product_version(result)
        result_sha256 = sha256_file(result_path)

        for existing in TopicJob.objects.filter(pipeline=TopicJob.Pipeline.INTEREST_V1, product_version=product_version):
            staging = existing.runtime_state.get("interestCandidateStaging")
            if isinstance(staging, dict) and staging.get("resultManifestSha256") == result_sha256:
                if existing.interest_candidates.count() != len(rows):
                    raise CommandError("existing staged candidate count does not match the verified package")
                self.stdout.write(json.dumps({
                    "ok": True,
                    "created": False,
                    "jobId": str(existing.id),
                    "candidates": len(rows),
                    "modelRecommended": sum(row["model_recommended"] for row in rows),
                    "snapshotsPublished": InterestSnapshot.objects.filter(job=existing).count(),
                }, ensure_ascii=True))
                return

        actor = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("date_joined").first()
        if actor is None:
            raise CommandError("an active superuser is required for candidate staging")

        job = TopicJob(
            created_by=actor,
            product_version=product_version,
            data_date=dates[-1],
            original_name=result_path.name,
            pipeline=TopicJob.Pipeline.INTEREST_V1,
            expected_size=result_path.stat().st_size,
            received_size=result_path.stat().st_size,
            expected_sha256=result_sha256,
            input_sha256=result_sha256,
            status=TopicJob.Status.INTEREST_VERIFYING,
            progress=95,
            started_at=timezone.now(),
        )
        root = job_directory(job.id, create=True).resolve()
        result_copy = safe_job_path(job.id, "interest/review/teeni-interest-batch-result.json", create_parent=True)
        candidate_copy = safe_job_path(job.id, "interest/review/teeni-interest-batch-candidates.private.json", create_parent=True)
        try:
            shutil.copy2(result_path, result_copy)
            shutil.copy2(candidate_path, candidate_copy)
            job.runtime_state = {
                "interestCandidateStaging": {
                    "dates": [value.isoformat() for value in dates],
                    "productVersion": product_version,
                    "sceneId": job.scene_id,
                    "registrySha256": result["registrySha256"],
                    "candidateDegraded": result.get("candidateDegraded") is True,
                    "candidateReviewComplete": False,
                    "resultManifestSha256": result_sha256,
                    "candidateSha256": sha256_file(candidate_copy),
                    "resultPath": result_copy.relative_to(root).as_posix(),
                    "candidatePath": candidate_copy.relative_to(root).as_posix(),
                },
            }
            ensure_seed_registry()
            with transaction.atomic():
                job.save(force_insert=True)
                InterestCandidate.objects.bulk_create([
                    InterestCandidate(job=job, data_date=dates[-1], **row)
                    for row in rows
                ])
        except Exception:
            jobs_root = (storage_root() / "jobs").resolve()
            if root.parent == jobs_root and root.is_dir():
                shutil.rmtree(root)
            raise

        if TopicArtifact.objects.filter(job=job).exists() or InterestSnapshot.objects.filter(job=job).exists():
            raise CommandError("candidate staging unexpectedly created publish artifacts")
        self.stdout.write(json.dumps({
            "ok": True,
            "created": True,
            "jobId": str(job.id),
            "candidates": len(rows),
            "modelRecommended": sum(row["model_recommended"] for row in rows),
            "snapshotsPublished": 0,
        }, ensure_ascii=True))

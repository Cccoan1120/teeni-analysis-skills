from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

from django.db import transaction

from interest_engine import ENGINE_VERSION, SNAPSHOT_SCHEMA
from interest_engine.analysis import CANDIDATE_SCHEMA
from interest_engine.contracts import sha256_file
from interest_engine.segments import (
    DEFAULT_PROVINCE_MAP,
    DIMENSION_SETS,
    ProvinceMap,
    SEGMENT_SCHEMA,
    dimension_set_id,
    validate_segment_payload,
)
from interest_engine.verify import verify
from interests.product_versions import payload_product_version
from interests.models import InterestCandidate, InterestSnapshot
from interests.segment_store import replace_segment_snapshots
from topics.models import TopicArtifact, TopicJob
from topics.storage import job_directory, safe_job_path


FORBIDDEN_KEYS = {
    "query",
    "text",
    "ai_text",
    "clientId",
    "cid",
    "timestamp",
    "created_at",
    "response",
    "context",
    "examples",
    "user_key",
    "session_key",
    "source_row",
    "profile_age",
    "profile_gender",
    "age_status",
    "gender_status",
    "birthday",
    "city",
    "city_normalized",
    "city_status",
    "region_id",
    "province",
}
LEGACY_SNAPSHOT_SCHEMA = "teeni-interest-snapshot/1.1.0"
LEGACY_ENGINE_VERSION = "teeni-interest-engine/1.1.0"
PREVIOUS_SNAPSHOT_SCHEMA = "teeni-interest-snapshot/1.2.0"
PREVIOUS_ENGINE_VERSION = "teeni-interest-engine/1.2.0"
EXACT_AGE_SNAPSHOT_SCHEMA = "teeni-interest-snapshot/1.3.0"
EXACT_AGE_ENGINE_VERSION = "teeni-interest-engine/1.3.0"
AGES = tuple(range(1, 18))
LEGACY_AGE_BANDS = ("1-2岁", "3-4岁", "5-6岁", "7-9岁", "10-17岁")
GENDERS = ("男", "女")
DEMOGRAPHIC_SAMPLE_MIN_USERS = 30


class InterestSnapshotContractError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InterestSnapshotContractError(message)


def assert_aggregate_only(value) -> None:
    if isinstance(value, dict):
        blocked = FORBIDDEN_KEYS.intersection(value)
        _require(not blocked, f"interest aggregate contains forbidden keys: {', '.join(sorted(blocked))}")
        for child in value.values():
            assert_aggregate_only(child)
    elif isinstance(value, list):
        for child in value:
            assert_aggregate_only(child)


def _require_rate(actual, numerator: int, denominator: int, label: str) -> None:
    if denominator == 0:
        _require(actual is None, f"{label} must be null for a zero denominator")
        return
    _require(isinstance(actual, (int, float)), f"{label} is invalid")
    _require(abs(actual - numerator / denominator) < 1e-12, f"{label} does not reconcile")


def _validate_demographic_interest(payload: dict, *, exact_age: bool) -> None:
    demographic = payload.get("demographicInterest")
    _require(isinstance(demographic, dict), "demographic interest aggregate is missing")
    overall_eligible = demographic.get("overallEligibleUsers")
    _require(isinstance(overall_eligible, int) and overall_eligible >= 0, "overall eligible users are invalid")
    rollup_ids = [row.get("id") for row in payload.get("ipRollups", [])]
    _require(len(rollup_ids) == len(set(rollup_ids)), "interest IP rollups contain duplicate ids")
    overall_ips = demographic.get("overallIps")
    _require(isinstance(overall_ips, list), "demographic overall IP metrics are missing")
    overall_ids = [row.get("ipId") for row in overall_ips if isinstance(row, dict)]
    _require(len(overall_ids) == len(overall_ips) and set(overall_ids) == set(rollup_ids), "demographic overall IP alignment mismatch")
    for row in overall_ips:
        interest_users = row.get("interestUsers")
        _require(
            isinstance(interest_users, int) and 0 <= interest_users <= overall_eligible,
            "demographic overall interest users are invalid",
        )
        _require_rate(row.get("overallCoverage"), interest_users, overall_eligible, "overall coverage")
    _require(
        overall_ips == sorted(
            overall_ips,
            key=lambda row: (-row["interestUsers"], -(row["overallCoverage"] or 0), row["ipId"]),
        ),
        "demographic overall IP order is invalid",
    )
    overall_by_id = {row["ipId"]: row for row in overall_ips}
    groups = demographic.get("groups")
    ages = AGES if exact_age else LEGACY_AGE_BANDS
    age_key = "age" if exact_age else "ageBand"
    expected_groups = [(age, gender) for age in ages for gender in GENDERS]
    _require(
        isinstance(groups, list) and len(groups) == len(expected_groups),
        f"demographic groups must contain {len(expected_groups)} fixed groups",
    )
    _require(
        [(row.get(age_key), row.get("gender")) for row in groups if isinstance(row, dict)] == expected_groups,
        "demographic group order or labels are invalid",
    )
    eligible_total = 0
    for group in groups:
        group_users = group.get("groupUsers")
        eligible_users = group.get("eligibleUsers")
        _require(isinstance(group_users, int) and group_users >= 0, "demographic group users are invalid")
        _require(isinstance(eligible_users, int) and 0 <= eligible_users <= group_users, "demographic eligible users are invalid")
        eligible_total += eligible_users
        expected_status = "可描述" if eligible_users >= DEMOGRAPHIC_SAMPLE_MIN_USERS else "样本不足"
        _require(group.get("sampleStatus") == expected_status, "demographic sample status is invalid")
        ips = group.get("ips")
        _require(isinstance(ips, list), "demographic group IP metrics are missing")
        _require([row.get("ipId") for row in ips] == overall_ids, "demographic group IP alignment mismatch")
        for row in ips:
            interest_users = row.get("interestUsers")
            _require(
                isinstance(interest_users, int) and 0 <= interest_users <= eligible_users,
                "demographic group interest users are invalid",
            )
            group_coverage = row.get("groupCoverage")
            _require_rate(group_coverage, interest_users, eligible_users, "group coverage")
            overall_coverage = overall_by_id[row["ipId"]]["overallCoverage"]
            difference = row.get("percentagePointDifference")
            if group_coverage is None or overall_coverage is None:
                _require(difference is None, "percentage-point difference must be null without both denominators")
            else:
                _require(
                    isinstance(difference, (int, float)) and abs(difference - (group_coverage - overall_coverage)) < 1e-12,
                    "percentage-point difference does not reconcile",
                )
    _require(eligible_total == overall_eligible, "demographic eligible user totals do not reconcile")


def _validate_segment_metadata(payload: dict) -> None:
    metadata = payload.get("segmentInterest")
    _require(isinstance(metadata, dict), "segment interest metadata is missing")
    _require(metadata.get("schemaVersion") == SEGMENT_SCHEMA, "segment interest schema mismatch")
    _require(metadata.get("sampleMinimumUsers") == DEMOGRAPHIC_SAMPLE_MIN_USERS, "segment sample threshold mismatch")
    domains = metadata.get("domains")
    _require(isinstance(domains, dict) and domains.get("ages") == list(AGES), "segment age domain mismatch")
    _require(
        domains.get("genders") == [{"id": "male", "label": "男"}, {"id": "female", "label": "女"}],
        "segment gender domain mismatch",
    )
    _require(isinstance(domains.get("regions"), list) and len(domains["regions"]) == 34, "segment region domain mismatch")
    expected_sets = [
        {"id": dimension_set_id(dimensions), "dimensions": list(dimensions)}
        for dimensions in DIMENSION_SETS
    ]
    actual_sets = metadata.get("dimensionSets")
    _require(isinstance(actual_sets, list) and len(actual_sets) == 7, "segment dimension coverage mismatch")
    _require(all(isinstance(row, dict) for row in actual_sets), "segment dimension metadata is invalid")
    _require(
        [
            {"id": row.get("id"), "dimensions": row.get("dimensions")}
            for row in actual_sets if isinstance(row, dict)
        ] == expected_sets,
        "segment dimension order mismatch",
    )
    _require(all(isinstance(row.get("groupCount"), int) and row["groupCount"] > 0 for row in actual_sets), "segment group counts are invalid")


def validate_interest_payload(payload: dict, job: TopicJob) -> None:
    _require(isinstance(payload, dict), "interest snapshot must be an object")
    _require(payload_product_version(payload) == job.product_version, "interest product version mismatch")
    schema_version = payload.get("schemaVersion")
    _require(
        schema_version in {SNAPSHOT_SCHEMA, "teeni-interest-snapshot/1.4.0", EXACT_AGE_SNAPSHOT_SCHEMA, PREVIOUS_SNAPSHOT_SCHEMA, LEGACY_SNAPSHOT_SCHEMA},
        "interest snapshot schema mismatch",
    )
    expected_engine = {
        SNAPSHOT_SCHEMA: ENGINE_VERSION,
        "teeni-interest-snapshot/1.4.0": "teeni-interest-engine/1.4.1",
        EXACT_AGE_SNAPSHOT_SCHEMA: EXACT_AGE_ENGINE_VERSION,
        PREVIOUS_SNAPSHOT_SCHEMA: PREVIOUS_ENGINE_VERSION,
        LEGACY_SNAPSHOT_SCHEMA: LEGACY_ENGINE_VERSION,
    }[schema_version]
    legacy_m1_engine = schema_version == "teeni-interest-snapshot/1.4.0" and job.product_version == "M1" and payload.get("engineVersion") == "teeni-interest-engine/1.4.0"
    _require(payload.get("engineVersion") == expected_engine or legacy_m1_engine, "interest engine version mismatch")
    _require(payload.get("dataDate") == str(job.data_date), "interest data date mismatch")
    _require(re.fullmatch(r"[0-9a-f]{64}", str(payload.get("sourceSha256", ""))) is not None, "invalid source hash")
    _require(re.fullmatch(r"[0-9a-f]{64}", str(payload.get("registrySha256", ""))) is not None, "invalid registry hash")
    totals = payload.get("totals")
    _require(isinstance(totals, dict), "interest totals are missing")
    for key in ("inputQueries", "validContentQueries", "entityQueries", "activeInterestUsers"):
        _require(isinstance(totals.get(key), int) and totals[key] >= 0, f"invalid interest total: {key}")
    _require(totals["entityQueries"] <= totals["validContentQueries"] <= totals["inputQueries"], "interest totals do not reconcile")
    _require(isinstance(payload.get("entities"), list), "interest entities are missing")
    _require(isinstance(payload.get("ipRollups"), list), "interest IP rollups are missing")
    _require(isinstance(payload.get("restrictedEntities"), list), "restricted interest aggregates are missing")
    _require(isinstance(payload.get("entityStructures"), list), "interest entity structures are missing")
    _require(isinstance(payload.get("candidates"), list) and len(payload["candidates"]) <= 10, "candidate radar exceeds top-10")
    _require(isinstance(payload.get("topics"), list) and len(payload["topics"]) == 12, "broad topic coverage mismatch")
    expected_behaviors = 8 if payload.get("engineVersion") == "teeni-interest-engine/1.5.0" else 7
    _require(isinstance(payload.get("behaviors"), list) and len(payload["behaviors"]) == expected_behaviors, "behavior coverage mismatch")
    _require(isinstance(payload.get("routes"), list) and len(payload["routes"]) == 6, "cleaning route coverage mismatch")
    if schema_version in {SNAPSHOT_SCHEMA, "teeni-interest-snapshot/1.4.0"}:
        _validate_demographic_interest(payload, exact_age=True)
        _validate_segment_metadata(payload)
    elif schema_version == EXACT_AGE_SNAPSHOT_SCHEMA:
        _validate_demographic_interest(payload, exact_age=True)
    elif schema_version == PREVIOUS_SNAPSHOT_SCHEMA:
        _validate_demographic_interest(payload, exact_age=False)
    assert_aggregate_only(payload)


def publish_interest_job(
    job: TopicJob,
    *,
    interest_state: dict | None = None,
    frozen_state: dict | None = None,
    require_publishing: bool = True,
    replace_candidates: bool = True,
) -> InterestSnapshot:
    job.refresh_from_db()
    _require(
        job.pipeline in {TopicJob.Pipeline.SHADOW, TopicJob.Pipeline.INTEREST_V1},
        "job does not run the interest-v1 pipeline",
    )
    if require_publishing:
        _require(job.status == TopicJob.Status.PUBLISHING, "job must be in publishing state")
    interest = interest_state if interest_state is not None else job.runtime_state.get("interest", {})
    frozen = frozen_state if frozen_state is not None else job.runtime_state.get("interestRegistry", {})
    workbook = safe_job_path(job.id, interest.get("workbookPath", ""))
    detail = safe_job_path(job.id, interest.get("detailPath", ""))
    snapshot_path = safe_job_path(job.id, interest.get("snapshotPath", ""))
    candidate_path = safe_job_path(job.id, interest.get("candidatePath", ""))
    manifest_path = safe_job_path(job.id, interest.get("manifestPath", ""))
    segment_path_value = interest.get("segmentPath")
    segment_path = safe_job_path(job.id, segment_path_value) if segment_path_value else None
    registry_path = safe_job_path(job.id, frozen.get("registryPath", ""))
    base = job.runtime_state.get("base", {})
    base_detail_value = base.get("primaryDetailPath")
    _require(bool(base_detail_value), "base detail path is missing")
    verify(
        workbook,
        detail,
        snapshot_path,
        candidate_path,
        manifest_path,
        registry_path,
        base_detail_path=safe_job_path(job.id, base_detail_value),
        segment_path=segment_path,
    )
    private_detail_value = interest.get("privateDetailPath")
    if private_detail_value:
        private_detail = safe_job_path(job.id, private_detail_value)
        _require(private_detail.is_file(), "private original-text detail is missing")
        _require("artifacts" not in private_detail.parts, "private original-text detail cannot be downloadable")
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    candidates = json.loads(candidate_path.read_text(encoding="utf-8"))
    validate_interest_payload(payload, job)
    segments = None
    if payload.get("schemaVersion") == SNAPSHOT_SCHEMA:
        _require(segment_path is not None and segment_path.is_file(), "interest segment sidecar is missing")
        segments = json.loads(segment_path.read_text(encoding="utf-8"))
        province_map = ProvinceMap.load(DEFAULT_PROVINCE_MAP)
        validate_segment_payload(
            segments,
            data_date=str(job.data_date),
            source_sha256=payload["sourceSha256"],
            registry_sha256=payload["registrySha256"],
            province_map_sha256=province_map.sha256,
            rollup_ids=[row["id"] for row in payload["ipRollups"]],
        )
    _require(payload_product_version(candidates) == job.product_version, "candidate product version mismatch")
    _require(candidates.get("schemaVersion") == CANDIDATE_SCHEMA, "private candidate schema mismatch")
    _require(payload["registrySha256"] == frozen.get("registrySha256"), "frozen registry hash mismatch")

    archive_name = f"Teeni{job.product_version}兴趣安全明细_{job.data_date:%Y%m%d}.zip"
    archive_path = safe_job_path(job.id, f"artifacts/{archive_name}", create_parent=True)
    temporary = archive_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(detail, arcname=f"Teeni{job.product_version}兴趣安全明细_{job.data_date:%Y%m%d}.csv")
        temporary.replace(archive_path)
    finally:
        temporary.unlink(missing_ok=True)

    workbook_hash = sha256_file(workbook)
    archive_hash = sha256_file(archive_path)
    from interests.preferences import build_contribution, save_contribution

    contribution = build_contribution(safe_job_path(job.id, base_detail_value), detail, registry_path, payload)
    root = job_directory(job.id).resolve()
    with transaction.atomic():
        snapshot, _ = InterestSnapshot.objects.update_or_create(
            product_version=job.product_version,
            data_date=job.data_date,
            defaults={
                "job": job,
                "schema_version": payload["schemaVersion"],
                "engine_version": payload["engineVersion"],
                "source_sha256": payload["sourceSha256"],
                "registry_sha256": payload["registrySha256"],
                "workbook_sha256": workbook_hash,
                "payload": payload,
            },
        )
        save_contribution(payload, contribution)
        if segments is not None:
            replace_segment_snapshots(job, segments)
        job.artifacts.filter(
            kind__in=[TopicArtifact.Kind.INTEREST_WORKBOOK, TopicArtifact.Kind.INTEREST_DETAIL_ZIP]
        ).delete()
        TopicArtifact.objects.create(
            job=job,
            kind=TopicArtifact.Kind.INTEREST_WORKBOOK,
            relative_path=workbook.resolve().relative_to(root).as_posix(),
            download_name=f"Teeni_{job.product_version}_IP兴趣分析_{job.data_date:%Y%m%d}.xlsx",
            sha256=workbook_hash,
            byte_size=workbook.stat().st_size,
            verified=True,
            expires_at=job.expires_at,
        )
        TopicArtifact.objects.create(
            job=job,
            kind=TopicArtifact.Kind.INTEREST_DETAIL_ZIP,
            relative_path=archive_path.resolve().relative_to(root).as_posix(),
            download_name=archive_name,
            sha256=archive_hash,
            byte_size=archive_path.stat().st_size,
            verified=True,
            expires_at=job.expires_at,
        )
        if replace_candidates:
            InterestCandidate.objects.filter(job=job).delete()
            InterestCandidate.objects.bulk_create([
                InterestCandidate(
                    job=job,
                    data_date=job.data_date,
                    phrase=str(row.get("phrase", ""))[:80],
                    normalized_phrase=str(row.get("normalizedPhrase", ""))[:80],
                    query_count=int(row.get("queryCount", 0)),
                    users=int(row.get("users", 0)),
                    sessions=int(row.get("sessions", 0)),
                    seven_day_growth=row.get("sevenDayGrowth"),
                    suggested_name=str(row.get("suggestedName", ""))[:80],
                    suggested_type=str(row.get("suggestedType", ""))[:32],
                    suggested_subtype=str(row.get("suggestedSubtype", ""))[:32],
                    suggested_parent_name=str(row.get("suggestedParentName", ""))[:80],
                    suggested_aliases=[str(value)[:80] for value in row.get("suggestedAliases", [])[:10]],
                    model_confidence=str(row.get("modelConfidence", ""))[:16],
                    model_recommended=row.get("accepted") is True,
                )
                for row in candidates.get("candidates", [])
            ])
    return snapshot

from __future__ import annotations

import csv
import json
import re
import hashlib
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import ENGINE_VERSION
from .analysis import AnalysisArtifacts, analyze
from .history import history_maps
from .segments import DEFAULT_PROVINCE_MAP
from .contracts import InputContractError, SCENE_PRODUCTS, load_and_verify_input, product_identity, sha256_file
from .model import CandidateEnricher, LocalCandidatePassthroughEnricher, enrich_candidates
from .registry import EntityRegistry
from .report import write_workbook
from .verify import verify


BATCH_SCHEMA = "teeni-interest-batch/1.0.0"
BATCH_CANDIDATE_SCHEMA = "teeni-interest-batch-candidates/1.0.0"
BATCH_RESULT_SCHEMA = "teeni-interest-batch-result/1.0.0"


class BatchContractError(ValueError):
    pass


@dataclass(frozen=True)
class BatchEntry:
    data_date: date
    detail_path: Path
    manifest_path: Path
    detail_sha256: str
    manifest_sha256: str
    scene_id: str = "488"

    @property
    def product_version(self) -> str:
        return SCENE_PRODUCTS[self.scene_id]


@dataclass(frozen=True)
class BatchContract:
    manifest_path: Path
    registry_path: Path
    registry_sha256: str
    candidate_review_complete: bool
    entries: tuple[BatchEntry, ...]
    scene_id: str = "488"

    @property
    def product_version(self) -> str:
        return SCENE_PRODUCTS[self.scene_id]


@dataclass(frozen=True)
class BatchArtifacts:
    daily: tuple[AnalysisArtifacts, ...]
    candidate_path: Path
    manifest_path: Path
    candidate_degraded: bool


def _resolve(base: Path, value: object, label: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise BatchContractError(f"{label} is required")
    path = Path(raw)
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def load_batch_manifest(path: str | Path) -> BatchContract:
    manifest_path = Path(path).resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatchContractError("batch manifest is not valid UTF-8 JSON") from exc
    if payload.get("schemaVersion") != BATCH_SCHEMA:
        raise BatchContractError("unsupported interest batch schema")
    base = manifest_path.parent
    registry_path = _resolve(base, payload.get("registryPath"), "registryPath")
    if not registry_path.is_file():
        raise BatchContractError("registryPath does not exist")
    registry_sha256 = str(payload.get("registrySha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", registry_sha256) or sha256_file(registry_path) != registry_sha256:
        raise BatchContractError("registry SHA-256 mismatch")
    EntityRegistry.from_json(registry_path)

    packages = payload.get("packages")
    if not isinstance(packages, list) or not 1 <= len(packages) <= 7:
        raise BatchContractError("interest batch requires one to seven packages")
    entries: list[BatchEntry] = []
    for item in packages:
        if not isinstance(item, dict):
            raise BatchContractError("batch packages must be objects")
        try:
            data_date = date.fromisoformat(str(item.get("dataDate") or ""))
        except ValueError as exc:
            raise BatchContractError("batch dataDate is invalid") from exc
        detail_path = _resolve(base, item.get("detailPath"), "detailPath")
        base_manifest_path = _resolve(base, item.get("manifestPath"), "manifestPath")
        detail_sha256 = str(item.get("detailSha256") or "")
        base_manifest_sha256 = str(item.get("manifestSha256") or "")
        if not detail_path.is_file() or sha256_file(detail_path) != detail_sha256:
            raise BatchContractError(f"detail SHA-256 mismatch for {data_date.isoformat()}")
        if not base_manifest_path.is_file() or sha256_file(base_manifest_path) != base_manifest_sha256:
            raise BatchContractError(f"manifest SHA-256 mismatch for {data_date.isoformat()}")
        try:
            base_payload = json.loads(base_manifest_path.read_text(encoding="utf-8-sig"))
            scene_id = str(base_payload.get("sceneId"))
        except (OSError, ValueError, AttributeError) as exc:
            raise BatchContractError("base manifest is not valid JSON") from exc
        if scene_id not in SCENE_PRODUCTS:
            raise BatchContractError("interest batch requires scene 488 (M1) or 904 (M2)")
        entries.append(BatchEntry(
            data_date=data_date,
            detail_path=detail_path,
            manifest_path=base_manifest_path,
            detail_sha256=detail_sha256,
            manifest_sha256=base_manifest_sha256,
            scene_id=scene_id,
        ))
    if len({entry.scene_id for entry in entries}) != 1:
        raise BatchContractError("interest batch cannot mix products or scenes")
    scene_id = entries[0].scene_id
    if ("sceneId" in payload and str(payload["sceneId"]) != scene_id
            or "productVersion" in payload and payload["productVersion"] != SCENE_PRODUCTS[scene_id]):
        raise BatchContractError("batch product identity does not match source packages")
    entries.sort(key=lambda item: item.data_date)
    if len({item.data_date for item in entries}) != len(entries):
        raise BatchContractError("batch dates must be unique")
    if any(current.data_date - previous.data_date != timedelta(days=1) for previous, current in zip(entries, entries[1:])):
        raise BatchContractError("batch dates must be contiguous")
    return BatchContract(
        manifest_path=manifest_path,
        registry_path=registry_path,
        registry_sha256=registry_sha256,
        candidate_review_complete=payload.get("candidateReviewComplete") is True,
        entries=tuple(entries),
        scene_id=scene_id,
    )


def _verify_package_date(entry: BatchEntry) -> None:
    contract = load_and_verify_input(entry.detail_path, entry.manifest_path)
    if contract.scene_id != entry.scene_id:
        raise BatchContractError("batch source scene mismatch")
    if contract.detail_sha256 != entry.detail_sha256:
        raise BatchContractError(f"base manifest detail mismatch for {entry.data_date.isoformat()}")
    rows = 0
    with entry.detail_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if "created_at" not in (reader.fieldnames or []):
            raise BatchContractError(f"created_at is required for {entry.data_date.isoformat()}")
        for row in reader:
            rows += 1
            if str(row.get("sceneId", "")).strip() != entry.scene_id:
                raise BatchContractError("detail sceneId mismatch or mixed scenes")
            created_at = str(row.get("created_at") or "").strip()
            if len(created_at) < 10 or created_at[:10] != entry.data_date.isoformat():
                raise BatchContractError(f"created_at date mismatch for {entry.data_date.isoformat()}")
    if rows != contract.expected_rows:
        raise BatchContractError(f"detail row count mismatch for {entry.data_date.isoformat()}")


def _aggregate_candidates(daily: list[AnalysisArtifacts]) -> list[dict]:
    combined: dict[str, dict] = {}
    for artifact in daily:
        package = json.loads(artifact.candidate_path.read_text(encoding="utf-8"))
        data_date = package["dataDate"]
        for row in package.get("candidates", []):
            normalized = str(row.get("normalizedPhrase") or "")
            aggregate = combined.setdefault(normalized, {
                "phrase": row.get("phrase"),
                "normalizedPhrase": normalized,
                "queryCount": 0,
                "users": 0,
                "sessions": 0,
                "days": set(),
                "dailyEligible": False,
                "examples": [],
            })
            aggregate["queryCount"] += int(row.get("queryCount", 0))
            aggregate["users"] += int(row.get("users", 0))
            aggregate["sessions"] += int(row.get("sessions", 0))
            aggregate["days"].add(data_date)
            aggregate["dailyEligible"] = aggregate["dailyEligible"] or bool(row.get("dailyEligible"))
            for example in row.get("examples", [])[:3]:
                cleaned = re.sub(r"\b1[3-9]\d{9}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[已去标识]", str(example))[:160]
                if cleaned and cleaned not in aggregate["examples"] and len(aggregate["examples"]) < 3:
                    aggregate["examples"].append(cleaned)
    eligible = []
    for row in combined.values():
        days = sorted(row.pop("days"))
        row["daysPresent"] = len(days)
        row["firstSeenDate"] = days[0]
        row["lastSeenDate"] = days[-1]
        row["sevenDayGrowth"] = None
        if row["dailyEligible"] or (len(days) >= 2 and row["users"] >= 10 and row["sessions"] >= 12):
            eligible.append(row)
    eligible.sort(key=lambda row: (-row["users"], -row["sessions"], -row["queryCount"], row["normalizedPhrase"]))
    return eligible[:200]


def _rewrite_daily_candidates(
    artifact: AnalysisArtifacts,
    registry: EntityRegistry,
    enriched_by_phrase: dict[str, dict],
    degraded: bool,
    failure_code: str | None,
) -> None:
    private_package = json.loads(artifact.candidate_path.read_text(encoding="utf-8"))
    rows = []
    for row in private_package.get("candidates", []):
        suggestion = enriched_by_phrase.get(str(row.get("normalizedPhrase")))
        if not suggestion:
            continue
        rows.append({
            **row,
            "accepted": suggestion.get("accepted", False),
            "suggestedName": suggestion.get("suggestedName", row.get("phrase")),
            "suggestedType": suggestion.get("suggestedType", "其他"),
            "suggestedSubtype": suggestion.get("suggestedSubtype", "其他热梗"),
            "suggestedParentName": suggestion.get("suggestedParentName", ""),
            "suggestedAliases": suggestion.get("suggestedAliases", []),
            "modelConfidence": suggestion.get("modelConfidence", "未运行"),
        })
    private_package["degraded"] = degraded
    private_package["failureCode"] = failure_code
    private_package["candidates"] = rows
    artifact.candidate_path.write_text(json.dumps(private_package, ensure_ascii=False, indent=2), encoding="utf-8")

    snapshot = artifact.snapshot
    snapshot["candidates"] = [
        {key: value for key, value in row.items() if key != "examples"}
        for row in rows[:10]
    ]
    snapshot["method"]["candidateDegraded"] = degraded
    snapshot["method"]["candidateFailureCode"] = failure_code
    snapshot["method"]["candidateScope"] = "seven_day_batch"
    artifact.snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    segments = json.loads(artifact.segment_path.read_text(encoding="utf-8"))
    write_workbook(artifact.workbook_path, snapshot, registry, segments)

    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    paths = {
        "workbook": artifact.workbook_path,
        "detail": artifact.detail_path,
        "private_detail": artifact.private_detail_path,
        "snapshot": artifact.snapshot_path,
        "segments": artifact.segment_path,
        "candidates_private": artifact.candidate_path,
    }
    if not any(item["role"] == "workbook" for item in manifest["outputs"]):
        manifest["outputs"].append({"role": "workbook", "file": artifact.workbook_path.name})
    for item in manifest["outputs"]:
        if item["role"] in {"detail", "private_detail", "segments"}:
            continue
        path = paths[item["role"]]
        item["sha256"] = sha256_file(path)
        item["bytes"] = path.stat().st_size
    artifact.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _artifact_at(directory: Path) -> AnalysisArtifacts:
    return AnalysisArtifacts(
        directory / "teeni-interest-report.xlsx", directory / "teeni-interest-detail.safe.csv",
        directory / "teeni-interest-detail.csv", directory / "teeni-interest-snapshot.json",
        directory / "teeni-interest-candidates.private.json", directory / "teeni-interest-segments.json",
        directory / "teeni-interest-manifest.json",
        json.loads((directory / "teeni-interest-snapshot.json").read_text(encoding="utf-8")),
    )


def _runtime_identity() -> dict:
    # Source hashes invalidate a cache even when a release forgot to bump its version.
    directory = Path(__file__).parent
    return {
        "engineVersion": ENGINE_VERSION,
        "provinceMapSha256": sha256_file(DEFAULT_PROVINCE_MAP),
        "code": {path.name: sha256_file(path) for path in sorted(directory.glob("*.py"))},
        "candidateMinUsers": 5, "candidateMinSessions": 8,
        "candidateIncludeBelowThreshold": True,
    }


def _restore_cache(directory: Path, destination: Path, identity: dict) -> AnalysisArtifacts | None:
    try:
        receipt = json.loads((directory / "cache.json").read_text(encoding="utf-8"))
        if receipt["identity"] != identity:
            return None
        required = {"teeni-interest-detail.safe.csv", "teeni-interest-detail.csv",
                    "teeni-interest-snapshot.json", "teeni-interest-segments.json",
                    "teeni-interest-candidates.private.json", "teeni-interest-manifest.json"}
        if set(receipt["files"]) != required:
            return None
        for name, digest in receipt["files"].items():
            if sha256_file(directory / name) != digest:
                return None
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in required:
            shutil.copy2(directory / name, destination / name)
        return _artifact_at(destination)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _store_cache(directory: Path, artifact: AnalysisArtifacts, identity: dict) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".daily-", dir=directory.parent))
    try:
        paths = (artifact.detail_path, artifact.private_detail_path, artifact.snapshot_path,
                 artifact.segment_path, artifact.candidate_path, artifact.manifest_path)
        hashes = {}
        for path in paths:
            shutil.copy2(path, temporary / path.name)
            hashes[path.name] = sha256_file(path)
        (temporary / "cache.json").write_text(json.dumps({"identity": identity, "files": hashes}), encoding="utf-8")
        # A concurrent valid writer may win; its immutable content has the same identity.
        if not directory.exists():
            try:
                temporary.rename(directory)
            except FileExistsError:
                pass
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _apply_history(artifact: AnalysisArtifacts, history: list[dict]) -> None:
    entities, candidates = history_maps(history, artifact.snapshot)
    for section in ("entities", "ipRollups", "restrictedEntities"):
        for row in artifact.snapshot.get(section, []):
            baseline = entities.get(f"{section}:{row['id']}")
            row["sevenDayChange"] = (row["activeInterestUsers"] - baseline) / baseline if baseline else None
    package = json.loads(artifact.candidate_path.read_text(encoding="utf-8"))
    for row in package["candidates"]:
        baseline = candidates.get(row["normalizedPhrase"], {}).get("averageUsers", 0)
        growth = row["users"] / baseline if baseline else None
        row["sevenDayGrowth"] = growth
        row["dailyEligible"] = (row["users"] >= 5 and row["sessions"] >= 8) or (
            row["users"] >= 3 and growth is not None and growth >= 3)
    artifact.snapshot["candidates"] = [{k: v for k, v in row.items() if k != "examples"}
                                        for row in package["candidates"][:10]]
    artifact.candidate_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")


def analyze_batch(
    contract: BatchContract,
    output_root: str | Path,
    *,
    enricher: CandidateEnricher | None = None,
    cache_dir: str | Path | None = None,
    progress=None,
) -> BatchArtifacts:
    started = time.monotonic()
    def emit(stage, **values):
        if progress:
            progress(stage, elapsedSeconds=round(time.monotonic() - started, 2), **values)

    emit("validating_inputs", days=len(contract.entries))
    for entry in contract.entries:
        if entry.scene_id != contract.scene_id:
            raise BatchContractError("interest batch cannot mix products or scenes")
        _verify_package_date(entry)
    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    registry = EntityRegistry.from_json(contract.registry_path)
    daily: list[AnalysisArtifacts] = []
    history: list[dict] = []
    local_enricher = LocalCandidatePassthroughEnricher()
    runtime = _runtime_identity()
    for entry in contract.entries:
        identity = {**runtime, "dataDate": entry.data_date.isoformat(),
                    "sceneId": contract.scene_id, "productVersion": contract.product_version,
                    "detailSha256": entry.detail_sha256, "manifestSha256": entry.manifest_sha256,
                    "registrySha256": contract.registry_sha256}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cached = Path(cache_dir).resolve() / key if cache_dir else None
        day_output = output / entry.data_date.isoformat()
        artifact = _restore_cache(cached, day_output, identity) if cached else None
        emit("daily_cache_hit" if artifact else "daily_analysis", dataDate=entry.data_date.isoformat())
        if artifact is None:
            artifact = analyze(
                entry.detail_path,
                entry.manifest_path,
                contract.registry_path,
                day_output,
                data_date=entry.data_date.isoformat(),
                history=None,
                enricher=local_enricher,
                candidate_include_below_threshold=True,
                defer_workbook=True,
                progress=emit,
            )
            if cached:
                _store_cache(cached, artifact, identity)
        _apply_history(artifact, history)
        daily.append(artifact)
        history.append(artifact.snapshot)

    candidates = _aggregate_candidates(daily)
    enriched, degraded, failure_code = enrich_candidates(candidates, enricher)
    enriched_by_phrase = {str(row.get("normalizedPhrase")): row for row in enriched}
    for entry, artifact in zip(contract.entries, daily):
        emit("workbook_and_verification", dataDate=entry.data_date.isoformat())
        _rewrite_daily_candidates(artifact, registry, enriched_by_phrase, degraded, failure_code)
        verify(
            artifact.workbook_path,
            artifact.detail_path,
            artifact.snapshot_path,
            artifact.candidate_path,
            artifact.manifest_path,
            contract.registry_path,
            artifact.private_detail_path,
            entry.detail_path,
            artifact.segment_path,
        )

    candidate_path = output / "teeni-interest-batch-candidates.private.json"
    candidate_path.write_text(json.dumps({
        "schemaVersion": BATCH_CANDIDATE_SCHEMA,
        "sceneId": contract.scene_id,
        "productVersion": contract.product_version,
        "registrySha256": contract.registry_sha256,
        "degraded": degraded,
        "failureCode": failure_code,
        "candidateItemLimit": 200,
        "candidateBatchSize": 20,
        "candidateRequestLimit": 10,
        "candidateBatches": (len(enriched) + 19) // 20,
        "candidates": enriched,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    result_path = output / "teeni-interest-batch-result.json"
    result_path.write_text(json.dumps({
        "schemaVersion": BATCH_RESULT_SCHEMA,
        "sceneId": contract.scene_id,
        "productVersion": contract.product_version,
        "runtimeIdentity": runtime,
        "verified": True,
        "sourceManifestSha256": sha256_file(contract.manifest_path),
        "registrySha256": contract.registry_sha256,
        "candidateReviewComplete": contract.candidate_review_complete,
        "candidateDegraded": degraded,
        "dates": [entry.data_date.isoformat() for entry in contract.entries],
        "daily": [{
            "dataDate": artifact.snapshot["dataDate"],
            "sceneId": contract.scene_id,
            "productVersion": contract.product_version,
            "baseDetailPath": str(entry.detail_path),
            "baseDetailSha256": entry.detail_sha256,
            "snapshotPath": artifact.snapshot_path.relative_to(output).as_posix(),
            "snapshotSha256": sha256_file(artifact.snapshot_path),
            "segmentPath": artifact.segment_path.relative_to(output).as_posix(),
            "segmentSha256": sha256_file(artifact.segment_path),
            "manifestPath": artifact.manifest_path.relative_to(output).as_posix(),
            "manifestSha256": sha256_file(artifact.manifest_path),
        } for entry, artifact in zip(contract.entries, daily)],
        "candidatePath": candidate_path.relative_to(output).as_posix(),
        "candidateSha256": sha256_file(candidate_path),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    emit("analysis_complete", days=len(daily))
    return BatchArtifacts(tuple(daily), candidate_path, result_path, degraded)


def load_batch_result(path: str | Path, contract: BatchContract, *, progress=None) -> BatchArtifacts:
    """Revalidate portable artifacts against current source inputs without classification."""
    result_path = Path(path).resolve()
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if (payload.get("schemaVersion") != BATCH_RESULT_SCHEMA or payload.get("verified") is not True
                or product_identity(payload) != (contract.scene_id, contract.product_version)
                or payload.get("runtimeIdentity") != _runtime_identity()
                or payload.get("registrySha256") != contract.registry_sha256
                or payload.get("candidateReviewComplete") != contract.candidate_review_complete
                or payload.get("dates") != [entry.data_date.isoformat() for entry in contract.entries]
                or len(payload.get("daily", [])) != len(contract.entries)):
            raise BatchContractError("result provenance mismatch; analyze with the current runtime")
        def checked(value, digest):
            target = _resolve(result_path.parent, value, "result artifact")
            if not target.is_relative_to(result_path.parent) or sha256_file(target) != digest:
                raise BatchContractError("result artifact hash or path mismatch")
            return target
        candidate = checked(payload["candidatePath"], payload["candidateSha256"])
        daily = []
        for entry, row in zip(contract.entries, payload["daily"]):
            if product_identity(row) != (contract.scene_id, contract.product_version):
                raise BatchContractError("result daily product mismatch")
            if row["dataDate"] != entry.data_date.isoformat() or row["baseDetailSha256"] != entry.detail_sha256:
                raise BatchContractError("result source mismatch")
            manifest = checked(row["manifestPath"], row["manifestSha256"])
            artifact = _artifact_at(manifest.parent)
            if product_identity(artifact.snapshot) != (contract.scene_id, contract.product_version):
                raise BatchContractError("result snapshot product mismatch")
            if checked(row["snapshotPath"], row["snapshotSha256"]) != artifact.snapshot_path:
                raise BatchContractError("result snapshot path mismatch")
            if checked(row["segmentPath"], row["segmentSha256"]) != artifact.segment_path:
                raise BatchContractError("result segment path mismatch")
            if artifact.snapshot["dataDate"] != entry.data_date.isoformat():
                raise BatchContractError("result snapshot date mismatch")
            _verify_package_date(entry)
            if progress:
                progress("verifying_result", dataDate=entry.data_date.isoformat())
            verify(artifact.workbook_path, artifact.detail_path, artifact.snapshot_path,
                   artifact.candidate_path, artifact.manifest_path, contract.registry_path,
                   artifact.private_detail_path, entry.detail_path, artifact.segment_path)
            daily.append(artifact)
        return BatchArtifacts(tuple(daily), candidate, result_path, payload["candidateDegraded"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise BatchContractError(f"invalid interest result: {exc}") from exc

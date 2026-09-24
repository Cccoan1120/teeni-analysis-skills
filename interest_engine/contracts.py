from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


BASE_MANIFEST_SCHEMA = "teeni-base-bundle-manifest/1.0.0"
BASE_DETAIL_CONTRACT = "teeni-base-detail/1.2.0"
SCENE_PRODUCTS = {"488": "M1", "904": "M2"}
REQUIRED_DETAIL_FIELDS = {
    "source_row",
    "clientId",
    "cid",
    "sceneId",
    "text",
    "ai_text",
    "turn_index",
    "is_template",
    "is_invalid_turn",
    "invalid_reason",
    "profile_age",
    "profile_gender",
    "age_status",
    "gender_status",
    "city_normalized",
    "city_status",
}


class InputContractError(ValueError):
    pass


@dataclass(frozen=True)
class InputContract:
    detail_path: Path
    manifest_path: Path
    detail_sha256: str
    expected_rows: int
    scene_id: str
    contract_version: str
    core_version: str | None = None
    rules_version: str | None = None

    @property
    def product_version(self) -> str:
        return SCENE_PRODUCTS[self.scene_id]


def product_identity(payload: dict) -> tuple[str, str]:
    """Missing identity is the legacy M1 contract; partial identity is invalid."""
    if "sceneId" not in payload and "productVersion" not in payload:
        return "488", "M1"
    scene = str(payload.get("sceneId", ""))
    product = payload.get("productVersion")
    if scene not in SCENE_PRODUCTS or product != SCENE_PRODUCTS[scene]:
        raise InputContractError("interest productVersion and sceneId mismatch")
    return scene, product


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_and_verify_input(detail_path: str | Path, manifest_path: str | Path) -> InputContract:
    detail = Path(detail_path).resolve()
    manifest = Path(manifest_path).resolve()
    if not detail.is_file() or not manifest.is_file():
        raise InputContractError("detail and manifest must exist")

    try:
        payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputContractError("manifest is not valid UTF-8 JSON") from exc

    if payload.get("schemaVersion") != BASE_MANIFEST_SCHEMA:
        raise InputContractError("unsupported base manifest schema")
    if payload.get("contractVersion") != BASE_DETAIL_CONTRACT:
        raise InputContractError("interest v1 requires teeni-base-detail/1.2.0")
    scene_id = str(payload.get("sceneId"))
    if scene_id not in SCENE_PRODUCTS:
        raise InputContractError("interest analysis requires scene 488 (M1) or 904 (M2)")
    if "productVersion" in payload and payload["productVersion"] != SCENE_PRODUCTS[scene_id]:
        raise InputContractError("base manifest productVersion and sceneId mismatch")

    output = next(
        (item for item in payload.get("outputs", []) if item.get("role") == "primary_detail"),
        None,
    )
    if not output:
        raise InputContractError("manifest does not declare a primary detail")
    if output.get("file") != detail.name:
        raise InputContractError("detail filename does not match manifest")
    expected_hash = str(output.get("sha256", ""))
    actual_hash = sha256_file(detail)
    if expected_hash != actual_hash:
        raise InputContractError("detail SHA-256 does not match manifest")
    expected_rows = output.get("rows")
    if not isinstance(expected_rows, int) or isinstance(expected_rows, bool) or expected_rows <= 0:
        raise InputContractError("manifest detail row count is invalid")

    csv.field_size_limit(2**31 - 1)
    try:
        with detail.open("r", encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream), None)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InputContractError("detail is not a readable UTF-8 CSV") from exc
    missing = REQUIRED_DETAIL_FIELDS - set(header or [])
    if missing:
        raise InputContractError(f"detail is missing fields: {', '.join(sorted(missing))}")

    return InputContract(
        detail_path=detail,
        manifest_path=manifest,
        detail_sha256=actual_hash,
        expected_rows=expected_rows,
        scene_id=scene_id,
        contract_version=BASE_DETAIL_CONTRACT,
        core_version=payload.get("coreVersion"),
        rules_version=payload.get("rulesVersion"),
    )

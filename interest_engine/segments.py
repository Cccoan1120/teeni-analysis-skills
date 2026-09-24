from __future__ import annotations

import itertools
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .classifier import InterestSignal
from .contracts import InputContractError, product_identity, sha256_file


SEGMENT_SCHEMA = "teeni-interest-segments/1.0.0"
PROVINCE_MAP_SCHEMA = "teeni-city-province-map/1.0.0"
DEFAULT_PROVINCE_MAP = Path(__file__).resolve().parent / "resources" / "city-province-map-v1.json"
AGES = tuple(range(1, 18))
GENDERS = (("male", "男"), ("female", "女"))
DIMENSION_SETS = (
    ("age",),
    ("gender",),
    ("region",),
    ("age", "gender"),
    ("age", "region"),
    ("gender", "region"),
    ("age", "gender", "region"),
)
SAMPLE_MIN_USERS = 30


class SegmentContractError(ValueError):
    pass


@dataclass(frozen=True)
class ProvinceMap:
    path: Path
    version: str
    sha256: str
    regions: tuple[dict, ...]
    aliases: dict[str, str]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PROVINCE_MAP) -> "ProvinceMap":
        resolved = Path(path).resolve()
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SegmentContractError("province map is not valid UTF-8 JSON") from exc
        if payload.get("schemaVersion") != PROVINCE_MAP_SCHEMA:
            raise SegmentContractError("unsupported province map schema")
        regions = payload.get("regions")
        aliases = payload.get("aliases")
        if not isinstance(regions, list) or len(regions) != 34:
            raise SegmentContractError("province map must contain 34 regions")
        if not isinstance(aliases, dict):
            raise SegmentContractError("province map aliases are missing")
        region_ids = [row.get("id") for row in regions if isinstance(row, dict)]
        if len(region_ids) != 34 or len(set(region_ids)) != 34:
            raise SegmentContractError("province map region ids are invalid")
        if any(not re.fullmatch(r"[0-9]{2}", str(value or "")) for value in region_ids):
            raise SegmentContractError("province map region ids must be two digits")
        if any(value not in set(region_ids) for value in aliases.values()):
            raise SegmentContractError("province map alias references an unknown region")
        return cls(
            path=resolved,
            version=str(payload.get("mapVersion") or ""),
            sha256=sha256_file(resolved),
            regions=tuple(regions),
            aliases={str(key): str(value) for key, value in aliases.items()},
        )

    def region_for_city(self, city: object) -> str | None:
        return self.aliases.get(str(city or "").strip())


def dimension_set_id(dimensions: tuple[str, ...]) -> str:
    if dimensions not in DIMENSION_SETS:
        raise SegmentContractError("unsupported dimension set")
    return "_".join(dimensions)


def parse_dimension_set(value: object) -> tuple[str, ...]:
    dimensions = tuple(str(value or "").split("_"))
    if dimensions not in DIMENSION_SETS:
        raise SegmentContractError("unsupported dimension set")
    return dimensions


def _domains(province_map: ProvinceMap) -> dict:
    return {
        "ages": list(AGES),
        "genders": [{"id": value, "label": label} for value, label in GENDERS],
        "regions": [dict(row) for row in province_map.regions],
    }


def _values_for_dimension(dimension: str, domains: dict) -> tuple[object, ...]:
    if dimension == "age":
        return tuple(domains["ages"])
    if dimension == "gender":
        return tuple(row["id"] for row in domains["genders"])
    if dimension == "region":
        return tuple(row["id"] for row in domains["regions"])
    raise SegmentContractError("unknown dimension")


def group_key(dimensions: tuple[str, ...], values: dict[str, object]) -> str:
    return "~".join(str(values[dimension]) for dimension in dimensions)


def expected_groups(dimensions: tuple[str, ...], domains: dict) -> tuple[dict[str, object], ...]:
    value_lists = [_values_for_dimension(dimension, domains) for dimension in dimensions]
    return tuple(
        dict(zip(dimensions, values, strict=True))
        for values in itertools.product(*value_lists)
    )


def _where_for(dimensions: tuple[str, ...], alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return " AND ".join(f"{prefix}{dimension}_stable = 1" for dimension in dimensions)


def build_segment_payload(
    database: sqlite3.Connection,
    rollup_ids: list[str],
    province_map: ProvinceMap,
    *,
    data_date: str,
    source_sha256: str,
    registry_sha256: str,
    product_version: str = "M1",
    scene_id: str = "488",
) -> dict:
    product_identity({"productVersion": product_version, "sceneId": scene_id})
    domains = _domains(province_map)
    column_for = {"age": "age", "gender": "gender", "region": "region_id"}
    active_signals = (InterestSignal.INITIATION.value, InterestSignal.CONTINUATION.value)
    payload_sets: dict[str, dict] = {}

    for dimensions in DIMENSION_SETS:
        set_id = dimension_set_id(dimensions)
        columns = [column_for[dimension] for dimension in dimensions]
        column_sql = ", ".join(columns)
        stable_where = _where_for(dimensions)
        joined_where = _where_for(dimensions, "ud")
        overall_eligible = int(database.execute(
            f"SELECT COUNT(*) FROM user_demographic WHERE {stable_where} AND eligible = 1"
        ).fetchone()[0])
        overall_counts = dict(database.execute(
            f"""
            SELECT rm.rollup_id, COUNT(DISTINCT et.user_key)
            FROM rollup_member rm
            JOIN entity_turn et ON et.entity_id = rm.entity_id
            JOIN user_demographic ud ON ud.user_key = et.user_key
            WHERE {joined_where} AND ud.eligible = 1 AND et.signal IN (?, ?)
            GROUP BY rm.rollup_id
            """,
            active_signals,
        ))
        overall_ips = [
            {
                "ipId": rollup_id,
                "interestUsers": int(overall_counts.get(rollup_id, 0)),
                "overallCoverage": (
                    int(overall_counts.get(rollup_id, 0)) / overall_eligible
                    if overall_eligible else None
                ),
            }
            for rollup_id in rollup_ids
        ]
        overall_ips.sort(key=lambda row: (
            -row["interestUsers"],
            -(row["overallCoverage"] or 0),
            row["ipId"],
        ))

        group_counts: dict[tuple[object, ...], tuple[int, int]] = {}
        for row in database.execute(
            f"""
            SELECT {column_sql}, COUNT(*), SUM(eligible)
            FROM user_demographic
            WHERE {stable_where}
            GROUP BY {column_sql}
            """
        ):
            group_counts[tuple(row[: len(columns)])] = (int(row[-2]), int(row[-1] or 0))

        interest_counts: dict[tuple[object, ...], dict[str, int]] = {}
        for row in database.execute(
            f"""
            SELECT {', '.join(f'ud.{column}' for column in columns)}, rm.rollup_id,
                   COUNT(DISTINCT et.user_key)
            FROM rollup_member rm
            JOIN entity_turn et ON et.entity_id = rm.entity_id
            JOIN user_demographic ud ON ud.user_key = et.user_key
            WHERE {joined_where} AND ud.eligible = 1 AND et.signal IN (?, ?)
            GROUP BY {', '.join(f'ud.{column}' for column in columns)}, rm.rollup_id
            """,
            active_signals,
        ):
            values = tuple(row[: len(columns)])
            interest_counts.setdefault(values, {})[str(row[-2])] = int(row[-1])

        groups = []
        for values in expected_groups(dimensions, domains):
            value_tuple = tuple(values[dimension] for dimension in dimensions)
            group_users, eligible_users = group_counts.get(value_tuple, (0, 0))
            counts = interest_counts.get(value_tuple, {})
            groups.append({
                "groupKey": group_key(dimensions, values),
                "values": values,
                "groupUsers": group_users,
                "eligibleUsers": eligible_users,
                "sampleStatus": "可描述" if eligible_users >= SAMPLE_MIN_USERS else "样本不足",
                "ipCounts": [
                    {"ipId": row["ipId"], "interestUsers": counts[row["ipId"]]}
                    for row in overall_ips
                    if counts.get(row["ipId"], 0) > 0
                ],
            })
        payload_sets[set_id] = {
            "dimensions": list(dimensions),
            "overallEligibleUsers": overall_eligible,
            "overallIps": overall_ips,
            "groups": groups,
        }

    mapped_users = int(database.execute(
        "SELECT COUNT(*) FROM user_demographic WHERE region_stable = 1"
    ).fetchone()[0])
    unmapped_users = int(database.execute(
        "SELECT COUNT(*) FROM user_demographic WHERE region_unmapped = 1"
    ).fetchone()[0])
    return {
        "schemaVersion": SEGMENT_SCHEMA,
        "productVersion": product_version,
        "sceneId": scene_id,
        "dataDate": data_date,
        "sourceSha256": source_sha256,
        "registrySha256": registry_sha256,
        "provinceMapVersion": province_map.version,
        "provinceMapSha256": province_map.sha256,
        "sampleMinimumUsers": SAMPLE_MIN_USERS,
        "domains": domains,
        "mappingDiagnostics": {
            "mappedUsers": mapped_users,
            "unmappedUsers": unmapped_users,
        },
        "dimensionSets": payload_sets,
    }


def materialize_group(segment_set: dict, group: dict) -> dict:
    eligible_users = int(group["eligibleUsers"])
    counts = {row["ipId"]: int(row["interestUsers"]) for row in group.get("ipCounts", [])}
    ips = []
    for overall in segment_set["overallIps"]:
        interest_users = counts.get(overall["ipId"], 0)
        group_coverage = interest_users / eligible_users if eligible_users else None
        overall_coverage = overall["overallCoverage"]
        ips.append({
            "ipId": overall["ipId"],
            "interestUsers": interest_users,
            "groupCoverage": group_coverage,
            "percentagePointDifference": (
                group_coverage - overall_coverage
                if group_coverage is not None and overall_coverage is not None
                else None
            ),
        })
    return {
        "groupKey": group["groupKey"],
        "values": dict(group["values"]),
        "groupUsers": int(group["groupUsers"]),
        "eligibleUsers": eligible_users,
        "sampleStatus": "可描述" if eligible_users >= SAMPLE_MIN_USERS else "样本不足",
        "ips": ips,
    }


def legacy_demographic(segment_payload: dict) -> dict:
    segment_set = segment_payload["dimensionSets"]["age_gender"]
    gender_labels = dict(GENDERS)
    return {
        "overallEligibleUsers": segment_set["overallEligibleUsers"],
        "overallIps": segment_set["overallIps"],
        "groups": [
            {
                "age": group["values"]["age"],
                "gender": gender_labels[group["values"]["gender"]],
                "groupUsers": group["groupUsers"],
                "eligibleUsers": group["eligibleUsers"],
                "sampleStatus": group["sampleStatus"],
                "ips": materialize_group(segment_set, group)["ips"],
            }
            for group in segment_set["groups"]
        ],
    }


def validate_segment_payload(
    payload: dict,
    *,
    data_date: str | None = None,
    source_sha256: str | None = None,
    registry_sha256: str | None = None,
    province_map_sha256: str | None = None,
    rollup_ids: list[str] | None = None,
    product_version: str | None = None,
    scene_id: str | None = None,
) -> None:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != SEGMENT_SCHEMA:
        raise SegmentContractError("interest segment schema mismatch")
    try:
        actual_scene, actual_product = product_identity(payload)
    except InputContractError as exc:
        raise SegmentContractError("interest segment product identity mismatch") from exc
    for actual, expected, label in (
        (actual_product, product_version, "product version"),
        (actual_scene, scene_id, "scene"),
        (payload.get("dataDate"), data_date, "data date"),
        (payload.get("sourceSha256"), source_sha256, "source hash"),
        (payload.get("registrySha256"), registry_sha256, "registry hash"),
        (payload.get("provinceMapSha256"), province_map_sha256, "province map hash"),
    ):
        if expected is not None and actual != expected:
            raise SegmentContractError(f"interest segment {label} mismatch")
    if payload.get("sampleMinimumUsers") != SAMPLE_MIN_USERS:
        raise SegmentContractError("interest segment sample threshold mismatch")
    domains = payload.get("domains")
    if not isinstance(domains, dict) or domains.get("ages") != list(AGES):
        raise SegmentContractError("interest segment age domain mismatch")
    if domains.get("genders") != [{"id": value, "label": label} for value, label in GENDERS]:
        raise SegmentContractError("interest segment gender domain mismatch")
    regions = domains.get("regions")
    if not isinstance(regions, list) or len(regions) != 34:
        raise SegmentContractError("interest segment region domain mismatch")
    expected_sets = {dimension_set_id(value) for value in DIMENSION_SETS}
    segment_sets = payload.get("dimensionSets")
    if not isinstance(segment_sets, dict) or set(segment_sets) != expected_sets:
        raise SegmentContractError("interest segment dimension set coverage mismatch")

    for dimensions in DIMENSION_SETS:
        set_id = dimension_set_id(dimensions)
        segment_set = segment_sets[set_id]
        if segment_set.get("dimensions") != list(dimensions):
            raise SegmentContractError(f"interest segment dimensions mismatch for {set_id}")
        overall_eligible = segment_set.get("overallEligibleUsers")
        if not isinstance(overall_eligible, int) or overall_eligible < 0:
            raise SegmentContractError(f"interest segment overall users invalid for {set_id}")
        overall_ips = segment_set.get("overallIps")
        if not isinstance(overall_ips, list):
            raise SegmentContractError(f"interest segment overall IPs missing for {set_id}")
        ids = [row.get("ipId") for row in overall_ips if isinstance(row, dict)]
        if len(ids) != len(overall_ips) or len(ids) != len(set(ids)):
            raise SegmentContractError(f"interest segment overall IP ids invalid for {set_id}")
        if rollup_ids is not None and set(ids) != set(rollup_ids):
            raise SegmentContractError(f"interest segment overall IP alignment mismatch for {set_id}")
        for row in overall_ips:
            interest_users = row.get("interestUsers")
            if not isinstance(interest_users, int) or not 0 <= interest_users <= overall_eligible:
                raise SegmentContractError(f"interest segment overall count invalid for {set_id}")
            expected_coverage = interest_users / overall_eligible if overall_eligible else None
            if row.get("overallCoverage") != expected_coverage:
                raise SegmentContractError(f"interest segment overall coverage mismatch for {set_id}")
        groups = segment_set.get("groups")
        expected_values = expected_groups(dimensions, domains)
        if not isinstance(groups, list) or len(groups) != len(expected_values):
            raise SegmentContractError(f"interest segment groups incomplete for {set_id}")
        for group, values in zip(groups, expected_values, strict=True):
            if group.get("values") != values or group.get("groupKey") != group_key(dimensions, values):
                raise SegmentContractError(f"interest segment group order mismatch for {set_id}")
            group_users = group.get("groupUsers")
            eligible_users = group.get("eligibleUsers")
            if not isinstance(group_users, int) or group_users < 0:
                raise SegmentContractError(f"interest segment group users invalid for {set_id}")
            if not isinstance(eligible_users, int) or not 0 <= eligible_users <= group_users:
                raise SegmentContractError(f"interest segment eligible users invalid for {set_id}")
            status = "可描述" if eligible_users >= SAMPLE_MIN_USERS else "样本不足"
            if group.get("sampleStatus") != status:
                raise SegmentContractError(f"interest segment sample status invalid for {set_id}")
            ip_counts = group.get("ipCounts")
            if not isinstance(ip_counts, list):
                raise SegmentContractError(f"interest segment sparse IP counts missing for {set_id}")
            sparse_ids = [row.get("ipId") for row in ip_counts if isinstance(row, dict)]
            if len(sparse_ids) != len(ip_counts) or len(sparse_ids) != len(set(sparse_ids)):
                raise SegmentContractError(f"interest segment sparse IP ids invalid for {set_id}")
            if not set(sparse_ids).issubset(ids):
                raise SegmentContractError(f"interest segment sparse IP alignment mismatch for {set_id}")
            for row in ip_counts:
                interest_users = row.get("interestUsers")
                if not isinstance(interest_users, int) or not 0 < interest_users <= eligible_users:
                    raise SegmentContractError(f"interest segment sparse IP count invalid for {set_id}")


def segment_summary_response(payload: dict, set_id: str) -> dict:
    dimensions = parse_dimension_set(set_id)
    segment_set = payload["dimensionSets"][set_id]
    return {
        "available": True,
        "schemaVersion": payload["schemaVersion"],
        "dataDate": payload["dataDate"],
        "dimensionSet": set_id,
        "dimensions": list(dimensions),
        "sampleMinimumUsers": payload["sampleMinimumUsers"],
        "domains": payload["domains"],
        "overallEligibleUsers": segment_set["overallEligibleUsers"],
        "overallIps": segment_set["overallIps"],
        "groups": [
            {key: value for key, value in group.items() if key != "ipCounts"}
            for group in segment_set["groups"]
        ],
    }


def segment_groups_response(payload: dict, set_id: str, keys: list[str]) -> dict:
    parse_dimension_set(set_id)
    if not 1 <= len(keys) <= 4 or len(set(keys)) != len(keys):
        raise SegmentContractError("groups must contain one to four unique values")
    segment_set = payload["dimensionSets"][set_id]
    by_key = {group["groupKey"]: group for group in segment_set["groups"]}
    if any(key not in by_key for key in keys):
        raise SegmentContractError("unknown segment group")
    return {
        "available": True,
        "schemaVersion": payload["schemaVersion"],
        "dataDate": payload["dataDate"],
        "dimensionSet": set_id,
        "overallEligibleUsers": segment_set["overallEligibleUsers"],
        "overallIps": segment_set["overallIps"],
        "groups": [materialize_group(segment_set, by_key[key]) for key in keys],
    }

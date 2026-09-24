from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import ENGINE_VERSION, REPORT_SCHEMA, SNAPSHOT_SCHEMA
from .candidates import candidate_phrases
from .classifier import Behavior, InterestSignal, QueryRoute, Turn, TurnContext, classify_turn, detail_evidence
from .contracts import InputContract, InputContractError, load_and_verify_input, product_identity, sha256_file
from .model import CandidateEnricher, enrich_candidates
from .history import history_maps
from .registry import BroadTopic, Entity, EntityRegistry, Visibility, is_standalone_ip, normalize_text
from .report import write_workbook
from .segments import (
    DEFAULT_PROVINCE_MAP,
    ProvinceMap,
    SEGMENT_SCHEMA,
    build_segment_payload,
    dimension_set_id,
    legacy_demographic,
    validate_segment_payload,
    DIMENSION_SETS,
)


DETAIL_SCHEMA = "teeni-interest-detail/1.2.0"
LEGACY_DETAIL_SCHEMA = "teeni-interest-detail/1.0.0"
PRIVATE_DETAIL_SCHEMA = "teeni-interest-private-detail/1.3.0"
PREVIOUS_PRIVATE_DETAIL_SCHEMA = "teeni-interest-private-detail/1.1.0"
LEGACY_PRIVATE_DETAIL_SCHEMA = "teeni-interest-private-detail/1.0.0"
CANDIDATE_SCHEMA = "teeni-interest-candidates/1.0.0"
MANIFEST_SCHEMA = "teeni-interest-manifest/1.7.0"
PREVIOUS_MANIFEST_SCHEMA = "teeni-interest-manifest/1.5.0"
EARLIER_MANIFEST_SCHEMA = "teeni-interest-manifest/1.4.0"
OLDER_MANIFEST_SCHEMA = "teeni-interest-manifest/1.2.0"
LEGACY_MANIFEST_SCHEMA = "teeni-interest-manifest/1.1.0"
INITIAL_MANIFEST_SCHEMA = "teeni-interest-manifest/1.0.0"
AGES = tuple(range(1, 18))
GENDERS = ("男", "女")
DEMOGRAPHIC_SAMPLE_MIN_USERS = 30
LEGACY_DETAIL_FIELDS = (
    "source_row",
    "user_key",
    "session_key",
    "route",
    "broad_topic",
    "behavior",
    "entity_ids",
    "interest_signal",
)
DETAIL_FIELDS = (
    "source_row",
    "user_key",
    "session_key",
    "route",
    "broad_topic",
    "behavior",
    "entity_names",
    "entity_ids",
    "interest_signal",
)
LEGACY_PRIVATE_DETAIL_FIELDS = (
    "source_row",
    "clientId",
    "cid",
    "created_at",
    "turn_index",
    "text",
    "ai_text",
    "route",
    "broad_topic",
    "behavior",
    "entity_ids",
    "interest_signal",
)
PREVIOUS_PRIVATE_DETAIL_FIELDS = (
    "source_row",
    "clientId",
    "cid",
    "created_at",
    "turn_index",
    "text",
    "ai_text",
    "route",
    "broad_topic",
    "behavior",
    "entity_names",
    "entity_ids",
    "interest_signal",
)
PRIVATE_DETAIL_FIELDS = (
    "source_row",
    "clientId",
    "cid",
    "created_at",
    "turn_index",
    "text",
    "ai_text",
    "route",
    "broad_topic",
    "behavior",
    "entity_names",
    "interest_signal",
)
EVIDENCE_FIELDS = ("entity_signals", "preference_polarities", "match_bases")
DETAIL_FIELDS += EVIDENCE_FIELDS
PRIVATE_DETAIL_FIELDS += EVIDENCE_FIELDS


@dataclass(frozen=True)
class AnalysisArtifacts:
    workbook_path: Path
    detail_path: Path
    private_detail_path: Path
    snapshot_path: Path
    candidate_path: Path
    segment_path: Path
    manifest_path: Path
    snapshot: dict


def age_for_profile(value: object) -> int | None:
    try:
        age = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return age if age in AGES else None


def _profile_dimensions(row: dict[str, str], client_id: str, province_map: ProvinceMap) -> dict[str, object]:
    if not client_id:
        return {
            "age": None, "age_stable": 0,
            "gender": None, "gender_stable": 0,
            "region_id": None, "region_stable": 0, "region_unmapped": 0,
        }
    age = age_for_profile(row.get("profile_age")) if row.get("age_status") == "正常" else None
    gender_label = str(row.get("profile_gender") or "").strip()
    gender = {"男": "male", "女": "female"}.get(gender_label) if row.get("gender_status") == "正常" else None
    city = str(row.get("city_normalized") or "").strip()
    city_normal = row.get("city_status") == "正常" and bool(city)
    region_id = province_map.region_for_city(city) if city_normal else None
    return {
        "age": age,
        "age_stable": 1 if age is not None else 0,
        "gender": gender,
        "gender_stable": 1 if gender is not None else 0,
        "region_id": region_id,
        "region_stable": 1 if region_id is not None else 0,
        "region_unmapped": 1 if city_normal and region_id is None else 0,
    }


def _key(prefix: str, value: str, detail_hash: str) -> str:
    return hashlib.sha256(f"{prefix}\0{detail_hash}\0{value}".encode("utf-8")).hexdigest()[:20]


def _open_work_database(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(path)
    database.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA temp_store=FILE;
        CREATE TABLE session_state (
            session_key TEXT PRIMARY KEY,
            user_entities TEXT NOT NULL,
            ai_entities TEXT NOT NULL
        );
        CREATE TABLE entity_turn (
            entity_id TEXT NOT NULL,
            user_key TEXT NOT NULL,
            session_key TEXT NOT NULL,
            query_key TEXT NOT NULL,
            signal TEXT NOT NULL,
            polarity TEXT NOT NULL
        );
        CREATE TABLE candidate_turn (
            normalized TEXT NOT NULL,
            phrase TEXT NOT NULL,
            user_key TEXT NOT NULL,
            session_key TEXT NOT NULL,
            example TEXT NOT NULL
        );
        CREATE TABLE user_demographic (
            user_key TEXT PRIMARY KEY,
            age INTEGER,
            age_stable INTEGER NOT NULL,
            gender TEXT,
            gender_stable INTEGER NOT NULL,
            region_id TEXT,
            region_stable INTEGER NOT NULL,
            region_unmapped INTEGER NOT NULL,
            eligible INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    return database


def _entity_metadata(entity: Entity, registry: EntityRegistry) -> dict:
    parent = registry.get(entity.parent_registry_id) if entity.parent_registry_id else None
    return {
        "id": entity.id,
        "name": entity.canonical_name,
        "parentId": parent.id if parent else None,
        "parentName": parent.canonical_name if parent else None,
        "entityType": entity.entity_type.value,
        "entitySubtype": entity.entity_subtype.value,
        "broadTopic": entity.broad_topic.value,
        "sourceLabel": entity.source_label,
        "sourcePlatform": entity.source_platform,
        "visibility": entity.visibility.value,
        "safetyCategory": entity.safety_category.value,
    }


def _metrics_for_ids(
    database: sqlite3.Connection,
    entity_ids: tuple[str, ...],
    *,
    baseline: float | None,
) -> dict:
    placeholders = ",".join("?" for _ in entity_ids)
    where = f"entity_id IN ({placeholders})"
    queries, mention_users, sessions, initiators, continuers = database.execute(
        f"""
        SELECT COUNT(DISTINCT query_key),
               COUNT(DISTINCT user_key),
               COUNT(DISTINCT session_key),
               COUNT(DISTINCT CASE WHEN signal = ? THEN user_key END),
               COUNT(DISTINCT CASE WHEN signal = ? THEN user_key END)
        FROM entity_turn WHERE {where}
        """,
        (InterestSignal.INITIATION.value, InterestSignal.CONTINUATION.value, *entity_ids),
    ).fetchone()
    active_users = database.execute(
        f"SELECT COUNT(DISTINCT user_key) FROM entity_turn WHERE {where} AND signal IN (?, ?)",
        (*entity_ids, InterestSignal.INITIATION.value, InterestSignal.CONTINUATION.value),
    ).fetchone()[0]
    deep_sessions = database.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT session_key FROM entity_turn WHERE {where}
            GROUP BY session_key HAVING COUNT(DISTINCT query_key) >= 3
        )
        """,
        entity_ids,
    ).fetchone()[0]
    polarities = {polarity: database.execute(
        f"SELECT COUNT(DISTINCT user_key) FROM entity_turn WHERE {where} AND polarity = ?",
        (*entity_ids, polarity),
    ).fetchone()[0] for polarity in ("positive", "negative", "neutral", "mixed")}
    return {
        "positivePreferenceUsers": polarities["positive"],
        "negativePreferenceUsers": polarities["negative"],
        "neutralMentionUsers": polarities["neutral"],
        "mixedPreferenceUsers": polarities["mixed"],
        "sampleUsers": mention_users,
        "sampleStatus": "可描述" if mention_users >= DEMOGRAPHIC_SAMPLE_MIN_USERS else "样本不足",
        "deepSampleSessions": sessions,
        "activeInterestUsers": active_users,
        "mentionUsers": mention_users,
        "initiatorUsers": initiators,
        "continuationUsers": continuers,
        "sessions": sessions,
        "queries": queries,
        "deepSessions": deep_sessions,
        "deepChatRate": deep_sessions / sessions if sessions else 0,
        "sevenDayChange": (active_users - baseline) / baseline if baseline else None,
    }


def _entity_rows(
    database: sqlite3.Connection,
    registry: EntityRegistry,
    history: dict[str, float],
    *,
    section: str,
    visibility: Visibility,
) -> list[dict]:
    database.executescript(
        """
        CREATE INDEX IF NOT EXISTS entity_turn_entity_user ON entity_turn(entity_id, user_key);
        CREATE INDEX IF NOT EXISTS entity_turn_entity_session ON entity_turn(entity_id, session_key);
        """
    )
    rows: list[dict] = []
    entity_ids = [row[0] for row in database.execute("SELECT DISTINCT entity_id FROM entity_turn")]
    for entity_id in entity_ids:
        entity = registry.get(entity_id)
        if entity.visibility != visibility:
            continue
        rows.append({
            **_entity_metadata(entity, registry),
            **_metrics_for_ids(database, (entity_id,), baseline=history.get(f"{section}:{entity_id}")),
        })
    rows.sort(key=lambda row: (-row["activeInterestUsers"], -row["mentionUsers"], -row["queries"], row["id"]))
    return rows


def _rollup_members(registry: EntityRegistry, matched: set[str]) -> dict[str, tuple[str, ...]]:
    parent_ids = {entity.parent_registry_id for entity in registry.entities if entity.parent_registry_id}
    standalone_ids = {
        entity.id
        for entity in registry.entities
        if entity.id in matched
        and entity.visibility == Visibility.PUBLIC
        and is_standalone_ip(entity)
    }
    members: dict[str, tuple[str, ...]] = {}
    for rollup_id in sorted({parent_id for parent_id in parent_ids if parent_id} | standalone_ids):
        rollup = registry.get(rollup_id)
        if rollup.visibility != Visibility.PUBLIC:
            continue
        is_parent = rollup_id in parent_ids
        member_ids = (
            tuple(
                entity.id
                for entity in registry.entities
                if entity.visibility == Visibility.PUBLIC
                and (entity.id == rollup_id or rollup_id in {ancestor.id for ancestor in registry.ancestors(entity.id)})
                and entity.id in matched
            )
            if is_parent
            else (rollup_id,)
        )
        if member_ids:
            members[rollup_id] = member_ids
    return members


def _rollup_rows(database: sqlite3.Connection, registry: EntityRegistry, history: dict[str, float]) -> list[dict]:
    matched = {row[0] for row in database.execute("SELECT DISTINCT entity_id FROM entity_turn")}
    rows: list[dict] = []
    parent_ids = {entity.parent_registry_id for entity in registry.entities if entity.parent_registry_id}
    for rollup_id, member_ids in _rollup_members(registry, matched).items():
        rollup = registry.get(rollup_id)
        is_parent = rollup_id in parent_ids
        child_count = len([entity_id for entity_id in member_ids if entity_id != rollup_id])
        rows.append({
            **_entity_metadata(rollup, registry),
            **_metrics_for_ids(
                database,
                member_ids,
                baseline=history.get(f"ipRollups:{rollup_id}"),
            ),
            "childEntityCount": child_count,
            "rollupKind": "parent" if is_parent else "standalone",
        })
    rows.sort(key=lambda row: (-row["activeInterestUsers"], -row["mentionUsers"], -row["queries"], row["id"]))
    return rows


def _prepare_rollup_members(
    database: sqlite3.Connection,
    registry: EntityRegistry,
    ip_rollups: list[dict],
) -> list[str]:
    rollup_ids = [str(row["id"]) for row in ip_rollups]
    matched = {row[0] for row in database.execute("SELECT DISTINCT entity_id FROM entity_turn")}
    members = _rollup_members(registry, matched)
    database.executescript(
        """
        CREATE TABLE rollup_member (
            rollup_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            PRIMARY KEY (rollup_id, entity_id)
        );
        CREATE INDEX entity_turn_user_signal ON entity_turn(user_key, signal, entity_id);
        CREATE INDEX user_demographic_age ON user_demographic(age_stable, age, eligible);
        CREATE INDEX user_demographic_gender ON user_demographic(gender_stable, gender, eligible);
        CREATE INDEX user_demographic_region ON user_demographic(region_stable, region_id, eligible);
        """
    )
    database.executemany(
        "INSERT INTO rollup_member(rollup_id, entity_id) VALUES (?, ?)",
        (
            (rollup_id, entity_id)
            for rollup_id in rollup_ids
            for entity_id in members.get(rollup_id, ())
        ),
    )
    return rollup_ids


def _structure_rows(database: sqlite3.Connection, registry: EntityRegistry) -> list[dict]:
    matched = {row[0] for row in database.execute("SELECT DISTINCT entity_id FROM entity_turn")}
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for entity in registry.entities:
        if entity.visibility == Visibility.PUBLIC and entity.id in matched:
            groups[(entity.entity_type.value, entity.entity_subtype.value)].append(entity.id)
    rows = []
    for (entity_type, entity_subtype), entity_ids in groups.items():
        rows.append({
            "name": f"{entity_type} / {entity_subtype}",
            "entityType": entity_type,
            "entitySubtype": entity_subtype,
            **_metrics_for_ids(database, tuple(entity_ids), baseline=None),
        })
        rows[-1].pop("sevenDayChange")
    rows.sort(key=lambda row: (-row["activeInterestUsers"], -row["mentionUsers"], -row["queries"], row["name"]))
    return rows


def _candidate_rows(
    database: sqlite3.Connection,
    history: dict[str, dict],
    min_users: int,
    min_sessions: int,
    *,
    include_below_threshold: bool = False,
) -> list[dict]:
    database.executescript(
        """
        CREATE INDEX candidate_turn_phrase ON candidate_turn(normalized);
        CREATE INDEX candidate_turn_user ON candidate_turn(normalized, user_key);
        CREATE INDEX candidate_turn_session ON candidate_turn(normalized, session_key);
        """
    )
    results: list[dict] = []
    aggregates = database.execute(
        """
        SELECT normalized, MIN(phrase), COUNT(*), COUNT(DISTINCT user_key), COUNT(DISTINCT session_key)
        FROM candidate_turn GROUP BY normalized
        """
    )
    for normalized, phrase, query_count, users, sessions in aggregates:
        baseline = float(history.get(normalized, {}).get("averageUsers", 0) or 0)
        growth = users / baseline if baseline > 0 else None
        daily_eligible = (users >= min_users and sessions >= min_sessions) or (
            users >= 3 and growth is not None and growth >= 3
        )
        if not daily_eligible and not include_below_threshold:
            continue
        examples = [
            row[0]
            for row in database.execute(
                "SELECT DISTINCT example FROM candidate_turn WHERE normalized = ? LIMIT 3",
                (normalized,),
            )
        ]
        results.append({
            "phrase": phrase,
            "normalizedPhrase": normalized,
            "queryCount": query_count,
            "users": users,
            "sessions": sessions,
            "sevenDayGrowth": growth,
            "dailyEligible": daily_eligible,
            "examples": examples,
        })
    results.sort(key=lambda row: (-row["users"], -row["sessions"], -row["queryCount"], row["normalizedPhrase"]))
    return results[:200]


def analyze(
    detail_path: str | Path,
    manifest_path: str | Path,
    registry_path: str | Path,
    output_dir: str | Path,
    *,
    data_date: str,
    history: list[dict] | None = None,
    enricher: CandidateEnricher | None = None,
    candidate_min_users: int = 5,
    candidate_min_sessions: int = 8,
    candidate_include_below_threshold: bool = False,
    defer_workbook: bool = False,
    progress=None,
) -> AnalysisArtifacts:
    contract: InputContract = load_and_verify_input(detail_path, manifest_path)
    for previous in history or []:
        if product_identity(previous) != (contract.scene_id, contract.product_version):
            raise InputContractError("interest history must belong to the same product and scene")
    registry_path = Path(registry_path).resolve()
    registry = EntityRegistry.from_json(registry_path)
    registry_hash = sha256_file(registry_path)
    province_map = ProvinceMap.load(DEFAULT_PROVINCE_MAP)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    detail_output = output / "teeni-interest-detail.safe.csv"
    private_detail_output = output / "teeni-interest-detail.csv"
    workbook_output = output / "teeni-interest-report.xlsx"
    snapshot_output = output / "teeni-interest-snapshot.json"
    candidate_output = output / "teeni-interest-candidates.private.json"
    segment_output = output / "teeni-interest-segments.json"
    manifest_output = output / "teeni-interest-manifest.json"
    work_database = output / ".interest-work.private.sqlite3"
    work_database.unlink(missing_ok=True)

    routes: Counter[str] = Counter()
    topics: Counter[str] = Counter()
    behaviors: Counter[str] = Counter()
    row_count = 0
    valid_content = 0
    entity_queries = 0
    database = _open_work_database(work_database)
    try:
        csv.field_size_limit(2**31 - 1)
        with (
            contract.detail_path.open("r", encoding="utf-8-sig", newline="") as source,
            detail_output.open("w", encoding="utf-8-sig", newline="") as target,
            private_detail_output.open("w", encoding="utf-8-sig", newline="") as private_target,
        ):
            reader = csv.DictReader(source)
            writer = csv.DictWriter(target, fieldnames=DETAIL_FIELDS)
            private_writer = csv.DictWriter(private_target, fieldnames=PRIVATE_DETAIL_FIELDS)
            writer.writeheader()
            private_writer.writeheader()
            for row in reader:
                if str(row.get("sceneId", "")).strip() != contract.scene_id:
                    raise InputContractError("detail sceneId mismatch or mixed scenes")
                row_count += 1
                client_id = str(row.get("clientId") or "")
                cid = str(row.get("cid") or "")
                source_row = str(row.get("source_row") or row_count)
                session_key = _key("session", cid or f"row:{source_row}", contract.detail_sha256)
                user_key = _key("user", client_id or f"session:{session_key}", contract.detail_sha256)
                demographic = _profile_dimensions(row, client_id, province_map)
                database.execute(
                    """
                    INSERT INTO user_demographic(
                        user_key, age, age_stable, gender, gender_stable,
                        region_id, region_stable, region_unmapped, eligible
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(user_key) DO UPDATE SET
                        age = CASE
                            WHEN user_demographic.age_stable = 1 AND excluded.age_stable = 1
                                 AND user_demographic.age = excluded.age
                            THEN user_demographic.age ELSE NULL END,
                        age_stable = CASE
                            WHEN user_demographic.age_stable = 1 AND excluded.age_stable = 1
                                 AND user_demographic.age = excluded.age
                            THEN 1 ELSE 0 END,
                        gender = CASE
                            WHEN user_demographic.gender_stable = 1 AND excluded.gender_stable = 1
                                 AND user_demographic.gender = excluded.gender
                            THEN user_demographic.gender ELSE NULL END,
                        gender_stable = CASE
                            WHEN user_demographic.gender_stable = 1 AND excluded.gender_stable = 1
                                 AND user_demographic.gender = excluded.gender
                            THEN 1 ELSE 0 END,
                        region_id = CASE
                            WHEN user_demographic.region_stable = 1 AND excluded.region_stable = 1
                                 AND user_demographic.region_id = excluded.region_id
                            THEN user_demographic.region_id ELSE NULL END,
                        region_stable = CASE
                            WHEN user_demographic.region_stable = 1 AND excluded.region_stable = 1
                                 AND user_demographic.region_id = excluded.region_id
                            THEN 1 ELSE 0 END,
                        region_unmapped = MAX(user_demographic.region_unmapped, excluded.region_unmapped)
                    """,
                    (
                        user_key,
                        demographic["age"],
                        demographic["age_stable"],
                        demographic["gender"],
                        demographic["gender_stable"],
                        demographic["region_id"],
                        demographic["region_stable"],
                        demographic["region_unmapped"],
                    ),
                )
                previous = database.execute(
                    "SELECT user_entities, ai_entities FROM session_state WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                context = TurnContext(
                    previous_entity_ids=tuple(previous[0].split("|")) if previous and previous[0] else (),
                    previous_ai_entity_ids=tuple(previous[1].split("|")) if previous and previous[1] else (),
                )
                result = classify_turn(
                    Turn(
                        text=str(row.get("text") or ""),
                        ai_text=str(row.get("ai_text") or ""),
                        is_template=str(row.get("is_template") or "否"),
                        is_invalid_turn=str(row.get("is_invalid_turn") or "否"),
                        invalid_reason=str(row.get("invalid_reason") or ""),
                    ),
                    context,
                    registry,
                )
                routes[result.route.value] += 1
                entity_ids = tuple(match.entity.id for match in result.entity_matches)
                entity_names = tuple(match.entity.canonical_name for match in result.entity_matches)
                if result.route == QueryRoute.VALID_CONTENT:
                    valid_content += 1
                    database.execute("UPDATE user_demographic SET eligible = 1 WHERE user_key = ?", (user_key,))
                    topics[(result.broad_topic or BroadTopic.OTHER).value] += 1
                    if result.behavior:
                        behaviors[result.behavior.value] += 1
                    if entity_ids:
                        entity_queries += 1
                        database.executemany(
                            "INSERT INTO entity_turn(entity_id, user_key, session_key, query_key, signal, polarity) VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                (entity_id, user_key, session_key, source_row, signal.value, polarity)
                                for entity_id, signal, polarity in zip(entity_ids, result.entity_signals, result.preference_polarities, strict=True)
                            ),
                        )
                    database.executemany(
                        "INSERT INTO candidate_turn(normalized, phrase, user_key, session_key, example) VALUES (?, ?, ?, ?, ?)",
                        (
                            (normalize_text(phrase), phrase, user_key, session_key, str(row.get("text") or "")[:160])
                            for phrase in candidate_phrases(str(row.get("text") or ""), tuple(match.alias.value for match in result.entity_matches))
                            if not registry.match(phrase)
                        ),
                    )
                writer.writerow({
                    **detail_evidence(result),
                    "source_row": source_row,
                    "user_key": user_key,
                    "session_key": session_key,
                    "route": result.route.value,
                    "broad_topic": result.broad_topic.value if result.broad_topic else "",
                    "behavior": result.behavior.value if result.behavior else "",
                    "entity_names": "|".join(entity_names),
                    "entity_ids": "|".join(entity_ids),
                    "interest_signal": result.interest_signal.value,
                })
                private_writer.writerow({
                    **detail_evidence(result),
                    "source_row": source_row,
                    "clientId": client_id,
                    "cid": cid,
                    "created_at": str(row.get("created_at") or ""),
                    "turn_index": str(row.get("turn_index") or ""),
                    "text": str(row.get("text") or ""),
                    "ai_text": str(row.get("ai_text") or ""),
                    "route": result.route.value,
                    "broad_topic": result.broad_topic.value if result.broad_topic else "",
                    "behavior": result.behavior.value if result.behavior else "",
                    "entity_names": "|".join(entity_names),
                    "interest_signal": result.interest_signal.value,
                })
                ai_entities = tuple(match.entity.id for match in registry.match(str(row.get("ai_text") or "")))
                database.execute(
                    """
                    INSERT INTO session_state(session_key, user_entities, ai_entities) VALUES (?, ?, ?)
                    ON CONFLICT(session_key) DO UPDATE SET user_entities = excluded.user_entities, ai_entities = excluded.ai_entities
                    """,
                    (
                        session_key,
                        "|".join(entity_ids) if result.route == QueryRoute.VALID_CONTENT else "",
                        "|".join(ai_entities),
                    ),
                )
                if row_count % 5000 == 0:
                    database.commit()
                    if progress:
                        progress("classification", dataDate=data_date, rows=row_count)
        database.commit()
        if row_count != contract.expected_rows:
            raise ValueError(f"detail row count mismatch: expected {contract.expected_rows}, got {row_count}")

        history_context = {
            "dataDate": data_date, "engineVersion": ENGINE_VERSION,
            "productVersion": contract.product_version, "sceneId": contract.scene_id,
            "schemaVersion": SNAPSHOT_SCHEMA, "detailSchema": DETAIL_SCHEMA,
            "registrySha256": registry_hash,
            "method": {"baseContract": contract.contract_version,
                       "baseCoreVersion": contract.core_version, "baseRulesVersion": contract.rules_version},
        }
        entity_history, candidate_history = history_maps(history, history_context)
        entities = _entity_rows(
            database,
            registry,
            entity_history,
            section="entities",
            visibility=Visibility.PUBLIC,
        )
        restricted_entities = _entity_rows(
            database,
            registry,
            entity_history,
            section="restrictedEntities",
            visibility=Visibility.RESTRICTED,
        )
        ip_rollups = _rollup_rows(database, registry, entity_history)
        rollup_ids = _prepare_rollup_members(database, registry, ip_rollups)
        segment_payload = build_segment_payload(
            database,
            rollup_ids,
            province_map,
            data_date=data_date,
            source_sha256=contract.detail_sha256,
            registry_sha256=registry_hash,
            product_version=contract.product_version,
            scene_id=contract.scene_id,
        )
        validate_segment_payload(
            segment_payload,
            data_date=data_date,
            source_sha256=contract.detail_sha256,
            registry_sha256=registry_hash,
            province_map_sha256=province_map.sha256,
            rollup_ids=rollup_ids,
            product_version=contract.product_version,
            scene_id=contract.scene_id,
        )
        demographic_interest = legacy_demographic(segment_payload)
        entity_structures = _structure_rows(database, registry)
        selected_candidates = _candidate_rows(
            database,
            candidate_history,
            candidate_min_users,
            candidate_min_sessions,
            include_below_threshold=candidate_include_below_threshold,
        )
        public_ids = tuple(entity.id for entity in registry.entities if entity.visibility == Visibility.PUBLIC)
        if public_ids:
            placeholders = ",".join("?" for _ in public_ids)
            entity_queries = database.execute(
                f"SELECT COUNT(DISTINCT query_key) FROM entity_turn WHERE entity_id IN ({placeholders})",
                public_ids,
            ).fetchone()[0]
            active_interest_users = database.execute(
                f"""
                SELECT COUNT(DISTINCT user_key) FROM entity_turn
                WHERE entity_id IN ({placeholders}) AND signal IN (?, ?)
                """,
                (*public_ids, InterestSignal.INITIATION.value, InterestSignal.CONTINUATION.value),
            ).fetchone()[0]
        else:
            entity_queries = 0
            active_interest_users = 0
    finally:
        database.close()
        work_database.unlink(missing_ok=True)
        work_database.with_name(work_database.name + "-wal").unlink(missing_ok=True)
        work_database.with_name(work_database.name + "-shm").unlink(missing_ok=True)

    enriched, candidate_degraded, candidate_failure = enrich_candidates(selected_candidates, enricher)
    candidate_private = enriched[:200]
    candidate_public = [
        {key: value for key, value in row.items() if key != "examples"}
        for row in candidate_private[:10]
    ]
    topic_rows = [
        {"name": topic.value, "queries": topics[topic.value], "rate": topics[topic.value] / valid_content if valid_content else 0}
        for topic in BroadTopic
    ]
    behavior_rows = [
        {"name": behavior.value, "queries": behaviors[behavior.value], "rate": behaviors[behavior.value] / valid_content if valid_content else 0}
        for behavior in Behavior
    ]
    route_rows = [
        {"name": route.value, "queries": routes[route.value], "rate": routes[route.value] / row_count if row_count else 0}
        for route in QueryRoute
    ]
    snapshot = {
        "productVersion": contract.product_version,
        "sceneId": contract.scene_id,
        "historyBaseline": history_context["historyBaseline"],
        "schemaVersion": SNAPSHOT_SCHEMA,
        "engineVersion": ENGINE_VERSION,
        "reportSchema": REPORT_SCHEMA,
        "detailSchema": DETAIL_SCHEMA,
        "dataDate": data_date,
        "sourceSha256": contract.detail_sha256,
        "registrySha256": registry_hash,
        "method": {
            "baseContract": contract.contract_version,
            "baseCoreVersion": contract.core_version,
            "baseRulesVersion": contract.rules_version,
            "registryVersion": registry.version,
            "candidateMode": "aggregate_review_only",
            "candidateRequestLimit": 10,
            "candidateItemLimit": 200,
            "candidateMinUsers": candidate_min_users,
            "candidateMinSessions": candidate_min_sessions,
            "candidateDegraded": candidate_degraded,
            "candidateFailureCode": candidate_failure,
        },
        "totals": {
            "inputQueries": row_count,
            "validContentQueries": valid_content,
            "entityQueries": entity_queries,
            "activeInterestUsers": active_interest_users,
        },
        "entities": entities,
        "ipRollups": ip_rollups,
        "demographicInterest": demographic_interest,
        "segmentInterest": {
            "schemaVersion": SEGMENT_SCHEMA,
            "provinceMapVersion": province_map.version,
            "provinceMapSha256": province_map.sha256,
            "sampleMinimumUsers": segment_payload["sampleMinimumUsers"],
            "domains": segment_payload["domains"],
            "dimensionSets": [
                {
                    "id": dimension_set_id(dimensions),
                    "dimensions": list(dimensions),
                    "groupCount": len(segment_payload["dimensionSets"][dimension_set_id(dimensions)]["groups"]),
                }
                for dimensions in DIMENSION_SETS
            ],
        },
        "restrictedEntities": restricted_entities,
        "entityStructures": entity_structures,
        "candidates": candidate_public,
        "topics": topic_rows,
        "behaviors": behavior_rows,
        "routes": route_rows,
    }
    snapshot_output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    segment_output.write_text(json.dumps(segment_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    candidate_output.write_text(
        json.dumps(
            {
                "schemaVersion": CANDIDATE_SCHEMA,
                "productVersion": contract.product_version,
                "sceneId": contract.scene_id,
                "dataDate": data_date,
                "degraded": candidate_degraded,
                "failureCode": candidate_failure,
                "candidates": candidate_private,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if not defer_workbook:
        write_workbook(workbook_output, snapshot, registry, segment_payload)
    outputs = []
    for role, path in (
        ("workbook", workbook_output),
        ("detail", detail_output),
        ("private_detail", private_detail_output),
        ("snapshot", snapshot_output),
        ("segments", segment_output),
        ("candidates_private", candidate_output),
    ):
        if role == "workbook" and defer_workbook:
            continue
        outputs.append({"role": role, "file": path.name, "sha256": sha256_file(path), "bytes": path.stat().st_size})
    manifest_output.write_text(
        json.dumps(
            {
                "schemaVersion": MANIFEST_SCHEMA,
                "productVersion": contract.product_version,
                "sceneId": contract.scene_id,
                "engineVersion": ENGINE_VERSION,
                "dataDate": data_date,
                "privateDetailSchema": PRIVATE_DETAIL_SCHEMA,
                "input": {"detailSha256": contract.detail_sha256, "rows": row_count},
                "registry": {"version": registry.version, "sha256": registry_hash},
                "provinceMap": {"version": province_map.version, "sha256": province_map.sha256},
                "outputs": outputs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return AnalysisArtifacts(
        workbook_path=workbook_output,
        detail_path=detail_output,
        private_detail_path=private_detail_output,
        snapshot_path=snapshot_output,
        candidate_path=candidate_output,
        segment_path=segment_output,
        manifest_path=manifest_output,
        snapshot=snapshot,
    )

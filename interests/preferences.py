"""Private daily contributions; only aggregate period metrics leave this module."""

import csv
from interest_engine.compatibility import interest_definition
import gzip
import hashlib
import hmac
import json
from collections import defaultdict
from datetime import date
from itertools import zip_longest
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from interest_engine.analysis import DETAIL_SCHEMA, _key, _profile_dimensions, _rollup_members
from interest_engine.classifier import InterestSignal, QueryRoute
from interest_engine.contracts import sha256_file
from interest_engine.registry import EntityRegistry
from interest_engine.segments import DEFAULT_PROVINCE_MAP, ProvinceMap, parse_dimension_set
from dashboard.product_versions import PRODUCT_VERSION_SCENES
from .product_versions import payload_product_version, versioned_request
from .models import InterestDailyContribution, InterestSnapshot

SCHEMA = "teeni-interest-preference-contribution/1.1.0"
LEGACY_SCHEMA = "teeni-interest-preference-contribution/1.0.0"


def _secret():
    return str(getattr(settings, "TEENI_INTEREST_IDENTITY_KEY", "") or settings.SECRET_KEY).encode()


def _identity():
    return hmac.new(_secret(), b"teeni-interest-preferences-v1", hashlib.sha256).hexdigest()


def _stable_key(kind, value):
    return hmac.new(_secret(), (kind + "\0" + value).encode(), hashlib.sha256).hexdigest()


def build_contribution(base_detail, safe_detail, registry_path, snapshot):
    version = payload_product_version(snapshot)
    scene_id = PRODUCT_VERSION_SCENES[version]
    base_detail, safe_detail = Path(base_detail), Path(safe_detail)
    source_hash = sha256_file(base_detail)
    if source_hash != snapshot["sourceSha256"]:
        raise ValueError("preference source hash mismatch")
    if sha256_file(Path(registry_path)) != snapshot["registrySha256"]:
        raise ValueError("preference registry hash mismatch")
    registry = EntityRegistry.from_json(registry_path)
    province_map = ProvinceMap.load(DEFAULT_PROVINCE_MAP)
    inverse = defaultdict(set)
    for ip, members in _rollup_members(registry, {entity.id for entity in registry.entities}).items():
        for entity in members:
            inverse[entity].add(ip)
    metadata = {row["id"]: {key: row.get(key) for key in ("id", "name", "entityType", "entitySubtype", "rollupKind")}
                for row in snapshot["ipRollups"]}
    users, hits = {}, {}
    rows = 0
    csv.field_size_limit(2**31 - 1)
    with base_detail.open(encoding="utf-8-sig", newline="") as base_stream, safe_detail.open(encoding="utf-8-sig", newline="") as safe_stream:
        for base, safe in zip_longest(csv.DictReader(base_stream), csv.DictReader(safe_stream)):
            if base is None or safe is None or base["source_row"] != safe["source_row"]:
                raise ValueError("preference detail join mismatch")
            if snapshot.get("detailSchema") == DETAIL_SCHEMA and any(field not in safe for field in ("entity_signals", "preference_polarities")):
                raise ValueError("current preference contribution requires per-entity evidence")
            rows += 1
            if str(base.get("sceneId", "488")) != scene_id:
                raise ValueError("preference source scene mismatch")
            if str(base.get("created_at", ""))[:10] != snapshot["dataDate"]:
                raise ValueError("preference date mismatch")
            client_id, cid = base.get("clientId", ""), base.get("cid", "")
            old_session = _key("session", cid or f"row:{base['source_row']}", source_hash)
            if safe["user_key"] != _key("user", client_id or f"session:{old_session}", source_hash) or safe["session_key"] != old_session:
                raise ValueError("preference pseudonym join mismatch")
            session = _stable_key("session" if version == "M1" else "session:" + version, cid or f"{source_hash}:{base['source_row']}")
            user = _stable_key("user" if version == "M1" else "user:" + version, client_id or f"session:{session}")
            dimensions = _profile_dimensions(base, client_id, province_map)
            profile = [dimensions["age"], dimensions["gender"], dimensions["region_id"]]
            eligible = safe["route"] == QueryRoute.VALID_CONTENT.value
            if user not in users:
                users[user] = [*profile, bool(eligible)]
            else:
                current = users[user]
                current[:3] = [a if a == b else None for a, b in zip(current[:3], profile)]
                current[3] = current[3] or eligible
            if not eligible:
                continue
            entity_ids = list(filter(None, safe["entity_ids"].split("|")))
            signals = safe.get("entity_signals", "").split("|") if "entity_signals" in safe else [safe["interest_signal"]] * len(entity_ids)
            polarities = safe.get("preference_polarities", "").split("|") if "preference_polarities" in safe else ["unknown"] * len(entity_ids)
            if not entity_ids:
                signals, polarities = [], []
            ips = defaultdict(list)
            for entity_id, signal, polarity in zip(entity_ids, signals, polarities, strict=True):
                registry.get(entity_id)
                for ip in inverse[entity_id]:
                    ips[ip].append((signal, polarity))
            for ip, evidence in ips.items():
                if ip not in metadata:
                    raise ValueError("preference rollup missing from snapshot")
                item = hits.setdefault((user, ip, session), [0, False, False, []])
                item[0] += 1
                item[1] = item[1] or any(signal == InterestSignal.INITIATION.value for signal, _ in evidence)
                item[2] = item[2] or any(signal == InterestSignal.CONTINUATION.value for signal, _ in evidence)
                item[3] = sorted(set(item[3]) | {polarity for _, polarity in evidence})
    if rows != snapshot["totals"]["inputQueries"]:
        raise ValueError("preference input row count mismatch")
    payload = {"schemaVersion": SCHEMA, "date": snapshot["dataDate"], "productVersion": version, "sceneId": scene_id, "users": users,
               "compatibility": {"engineVersion": snapshot.get("engineVersion"), "detailSchema": snapshot.get("detailSchema"),
                                 "baseCoreVersion": snapshot.get("method", {}).get("baseCoreVersion"),
                                 "baseRulesVersion": snapshot.get("method", {}).get("baseRulesVersion")},
               "hits": [[*key, *values] for key, values in hits.items()], "ips": metadata,
               "regions": list(province_map.regions), "rows": rows, "detailSha256": sha256_file(safe_detail)}
    # Check the new contribution against already independently verified daily IP totals.
    daily_metrics = _metrics(payload["hits"], set(users), metadata)
    for row in snapshot["ipRollups"]:
        actual = next((item for item in daily_metrics if item["id"] == row["id"]), None)
        for key in ("activeInterestUsers", "mentionUsers", "initiatorUsers", "continuationUsers", "queries", "sessions", "deepSessions"):
            if actual is None or actual[key] != row[key]:
                raise ValueError("preference daily metrics mismatch: " + key)
        for key in ("positivePreferenceUsers", "negativePreferenceUsers", "neutralMentionUsers", "mixedPreferenceUsers"):
            if key in row and actual[key] != row[key]:
                raise ValueError("preference daily direction mismatch: " + key)
    return payload


def save_contribution(snapshot, payload):
    version = payload_product_version(snapshot)
    if payload_product_version(payload) != version:
        raise ValueError("preference contribution version mismatch")
    if payload.get("schemaVersion") != SCHEMA or payload.get("date") != snapshot["dataDate"]:
        raise ValueError("preference contribution contract mismatch")
    return InterestDailyContribution.objects.update_or_create(
        product_version=version, data_date=snapshot["dataDate"], defaults={
            "source_sha256": snapshot["sourceSha256"], "registry_sha256": snapshot["registrySha256"],
            "identity_sha256": _identity(),
            "detail_sha256": payload["detailSha256"],
            "payload": gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0),
        },
    )[0]


def _metrics(hits, selected_users, metadata):
    grouped = {}
    for hit in hits:
        user, ip, session, queries, initiated, continued = hit[:6]
        polarities = hit[6] if len(hit) > 6 else ["unknown"]
        if user not in selected_users:
            continue
        item = grouped.setdefault(ip, {"users": set(), "initiators": set(), "continuers": set(), "sessions": defaultdict(int), "polarities": defaultdict(set)})
        item["users"].add(user)
        if initiated:
            item["initiators"].add(user)
        if continued:
            item["continuers"].add(user)
        item["sessions"][session] += queries
        for polarity in polarities:
            item["polarities"][polarity].add(user)
    result = []
    for ip, item in grouped.items():
        sessions = len(item["sessions"])
        deep = sum(count >= 3 for count in item["sessions"].values())
        result.append({**metadata[ip], "activeInterestUsers": len(item["initiators"] | item["continuers"]),
                       "positivePreferenceUsers": len(item["polarities"]["positive"]),
                       "negativePreferenceUsers": len(item["polarities"]["negative"]),
                       "neutralMentionUsers": len(item["polarities"]["neutral"]),
                       "mixedPreferenceUsers": len(item["polarities"]["mixed"]),
                       "preferenceUnknownUsers": len(item["polarities"]["unknown"]),
                       "sampleUsers": len(item["users"]), "deepSampleSessions": sessions,
                       "mentionUsers": len(item["users"]), "initiatorUsers": len(item["initiators"]),
                       "continuationUsers": len(item["continuers"]), "sessions": sessions,
                       "queries": sum(item["sessions"].values()), "deepSessions": deep,
                       "deepChatRate": deep / sessions if sessions else 0})
    return sorted(result, key=lambda row: (-row["activeInterestUsers"], -row["mentionUsers"], -row["queries"], row["id"]))


def aggregate_preferences(contributions, *, dimension_set="all", requested_group=None):
    dimensions = () if dimension_set == "all" else parse_dimension_set(dimension_set)
    users, hits, metadata, regions = {}, [], {}, {}
    selected_version = None
    compatibility = None
    for contribution in contributions:
        version = contribution.product_version
        if selected_version is not None and version != selected_version:
            raise ValueError("cannot aggregate interest contributions across product versions")
        selected_version = version
        payload = json.loads(gzip.decompress(bytes(contribution.payload)))
        if payload_product_version(payload) != version:
            raise ValueError("stored preference contribution version mismatch")
        if payload.get("schemaVersion") not in {SCHEMA, LEGACY_SCHEMA}:
            raise ValueError("unsupported preference contribution schema")
        current = (payload["schemaVersion"], contribution.identity_sha256, contribution.registry_sha256,
                   json.dumps(interest_definition(payload.get("compatibility", {})), sort_keys=True))
        if compatibility is not None and current != compatibility:
            raise ValueError("preference definitions differ across dates; rebuild compatible contributions")
        compatibility = current
        for user, profile in payload["users"].items():
            previous_eligible = users.get(user, [None, None, None, False])[3]
            users[user] = [*profile[:3], profile[3] or previous_eligible]
        hits.extend(payload["hits"])
        metadata.update(payload["ips"])
        regions.update({row["id"]: row["label"] for row in payload["regions"]})
    indexes = {"age": 0, "gender": 1, "region": 2}
    group_users = defaultdict(set)
    labels = {}
    for user, profile in users.items():
        values = [profile[indexes[dimension]] for dimension in dimensions]
        if any(value is None for value in values):
            continue
        key = "~".join(map(str, values)) if dimensions else "all"
        group_users[key].add(user)
        labels[key] = " / ".join(str(value) + "岁" if dimension == "age" else
                                {"male": "男", "female": "女"}.get(value, str(value)) if dimension == "gender" else
                                regions.get(value, str(value)) for dimension, value in zip(dimensions, values)) or "全部用户"
    def group_order(item):
        return tuple(int(value) if dimension == "age" else value
                     for dimension, value in zip(dimensions, item[0].split("~")))

    groups = [{"id": key, "label": labels[key], "groupUsers": len(members),
               "eligibleUsers": sum(bool(users[user][3]) for user in members)}
              for key, members in sorted(group_users.items(), key=group_order)]
    selected = next((item for item in groups if item["id"] == requested_group), None)
    if requested_group and selected is None:
        selected = {"id": requested_group, "label": requested_group, "groupUsers": 0, "eligibleUsers": 0}
    if selected is None:
        selected = max(groups, key=lambda item: (item["eligibleUsers"], item["groupUsers"]), default=None)
    members = group_users.get(selected["id"], set()) if selected else set()
    ips = _metrics(hits, members, metadata)
    active = {row[0] for row in hits if row[0] in members and (row[4] or row[5])}
    sessions = {row[2] for row in hits if row[0] in members}
    eligible = selected["eligibleUsers"] if selected else 0
    for row in ips:
        row["coverage"] = row["activeInterestUsers"] / eligible if eligible else None
        row["sampleStatus"] = "样本不足" if row["sampleUsers"] < 30 else "可描述"
        row["coverageSampleUsers"] = eligible
    return {"dimensionSet": dimension_set, "groups": groups, "selectedGroup": selected, "ips": ips,
            "totals": {"groupUsers": len(members), "eligibleUsers": eligible, "activeInterestUsers": len(active),
                       "sessions": len(sessions), "ipQueries": sum(row["queries"] for row in ips)},
            "profileMethod": "期末最近一次画像", "userMethod": "跨日期去重用户", "sampleMinimumUsers": 30}


@require_GET
@login_required
@versioned_request
def preferences(request):
    period = request.GET.get("period", "day")
    dimension_set = request.GET.get("dimensionSet", "all")
    records = InterestDailyContribution.objects.filter(product_version=request.product_version).order_by("data_date")
    available_dates = [value.isoformat() for value in records.values_list("data_date", flat=True)]
    available_months = sorted({value[:7] for value in available_dates})
    try:
        if period == "day":
            chosen = date.fromisoformat(request.GET.get("date") or (available_dates[-1] if available_dates else date.today().isoformat()))
            records = records.filter(data_date=chosen)
        elif period == "month":
            month = request.GET.get("month") or (available_months[-1] if available_months else date.today().strftime("%Y-%m"))
            if len(month) != 7:
                raise ValueError("month must use YYYY-MM")
            chosen = date.fromisoformat(month + "-01")
            records = records.filter(data_date__year=chosen.year, data_date__month=chosen.month)
        elif period != "all":
            raise ValueError("period must be day, month or all")
        if dimension_set != "all":
            parse_dimension_set(dimension_set)
    except ValueError as exc:
        return JsonResponse({"error": "invalid_preference_period", "detail": str(exc)}, status=400)
    revisions = list(records.values_list("data_date", "updated_at", "identity_sha256"))
    incompatible = [str(item[0]) for item in revisions if item[2] != _identity()]
    if incompatible:
        return JsonResponse({"error": "preference_identity_changed", "detail": "累计用户标识配置已变化，需重新构建历史贡献。"}, status=409)
    families = defaultdict(list)
    for record in records:
        contribution = json.loads(gzip.decompress(bytes(record.payload)))
        definition = (contribution.get("schemaVersion"), record.identity_sha256, record.registry_sha256,
                      json.dumps(interest_definition(contribution.get("compatibility", {})), sort_keys=True))
        families[definition].append(record)
    anchors = sorted(str(group[-1].data_date) for group in families.values())
    selected_anchor = request.GET.get("definitionDate") or (anchors[-1] if anchors else None)
    selected_records = next((group for group in families.values() if str(group[-1].data_date) == selected_anchor), [])
    if selected_anchor and not selected_records:
        return JsonResponse({"error": "invalid_definition_date", "detail": "该周期不存在所选分析口径。"}, status=400)
    selected_dates = {str(record.data_date) for record in selected_records}
    excluded_dates = [str(item[0]) for item in revisions if str(item[0]) not in selected_dates]
    key = "interest-preferences:v4:" + hashlib.sha256(json.dumps(
        [request.product_version, revisions, dimension_set, request.GET.get("groupKey"), selected_anchor], default=str).encode()).hexdigest()
    result = cache.get(key)
    if result is None:
        try:
            result = aggregate_preferences(selected_records, dimension_set=dimension_set, requested_group=request.GET.get("groupKey"))
        except ValueError:
            return JsonResponse({"error": "preference_definitions_changed", "detail": "所选日期的分析口径不兼容，需使用同一引擎与实体库重建历史贡献。"}, status=409)
        cache.set(key, result, 300)
    result = dict(result)
    dates = sorted(selected_dates)
    expected = InterestSnapshot.objects.filter(product_version=request.product_version).values_list("data_date", flat=True)
    if period == "day":
        expected = expected.filter(data_date=chosen)
    elif period == "month":
        expected = expected.filter(data_date__year=chosen.year, data_date__month=chosen.month)
    result.update({"schemaVersion": "teeni-interest-preferences/1.0.0", "period": period,
                   "productVersion": request.product_version, "sceneId": PRODUCT_VERSION_SCENES[request.product_version],
                   "availableDates": available_dates, "availableMonths": available_months, "dates": dates,
                   "startDate": dates[0] if dates else None, "endDate": dates[-1] if dates else None,
                   "definitionDates": anchors, "definitionDate": selected_anchor, "excludedIncompatibleDates": excluded_dates,
                   "coveredDays": len(dates), "missingDates": [str(value) for value in expected if str(value) not in dates and str(value) not in excluded_dates]})
    return JsonResponse(result)

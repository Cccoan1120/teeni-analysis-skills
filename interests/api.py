from __future__ import annotations

import json
from copy import deepcopy
from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from interest_engine.registry import BroadTopic, EntitySubtype, EntityType, SafetyCategory, Visibility
from interest_engine.segments import SegmentContractError, materialize_group, parse_dimension_set

from dashboard.product_versions import PRODUCT_VERSION_SCENES
from .product_versions import versioned_request
from .models import InterestAlias, InterestCandidate, InterestEntity, InterestSegmentSnapshot, InterestSnapshot
from .registry import ensure_seed_registry, review_candidate


def _error(code: str, detail: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": code, "detail": detail}, status=status)


def _body(request: HttpRequest) -> dict:
    value = json.loads(request.body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError
    return value


def serialize_candidate(candidate: InterestCandidate) -> dict:
    return {
        "id": str(candidate.id),
        "dataDate": candidate.data_date.isoformat(),
        "productVersion": candidate.job.product_version,
        "sceneId": candidate.job.scene_id,
        "phrase": candidate.phrase,
        "queryCount": candidate.query_count,
        "users": candidate.users,
        "sessions": candidate.sessions,
        "sevenDayGrowth": candidate.seven_day_growth,
        "suggestedName": candidate.suggested_name,
        "suggestedType": candidate.suggested_type,
        "suggestedSubtype": candidate.suggested_subtype,
        "suggestedParentName": candidate.suggested_parent_name,
        "suggestedAliases": candidate.suggested_aliases,
        "modelConfidence": candidate.model_confidence,
        "modelRecommended": candidate.model_recommended,
        "status": candidate.status,
        "resolvedRegistryId": candidate.resolved_entity.registry_id if candidate.resolved_entity_id else None,
    }


def serialize_entity(entity: InterestEntity) -> dict:
    return {
        "registryId": entity.registry_id,
        "canonicalName": entity.canonical_name,
        "entityType": entity.entity_type,
        "entitySubtype": entity.entity_subtype,
        "broadTopic": entity.broad_topic,
        "parentRegistryId": entity.parent.registry_id if entity.parent_id else None,
        "parentName": entity.parent.canonical_name if entity.parent_id else None,
        "sourceLabel": entity.source_label,
        "sourcePlatform": entity.source_platform,
        "visibility": entity.visibility,
        "safetyCategory": entity.safety_category,
        "aliases": [alias.value for alias in entity.aliases.all() if not alias.ambiguous],
        "ambiguousAliases": [alias.value for alias in entity.aliases.all() if alias.ambiguous],
    }


@require_GET
@login_required
@versioned_request
def list_interests(request: HttpRequest) -> JsonResponse:
    snapshots = InterestSnapshot.objects.select_related("job").filter(product_version=request.product_version)
    if request.GET.get("date"):
        snapshots = snapshots.filter(data_date=request.GET["date"])
    items = []
    for snapshot in snapshots:
        payload = deepcopy(snapshot.payload)
        payload.update({"productVersion": snapshot.product_version, "sceneId": snapshot.job.scene_id})
        if not request.user.is_staff:
            payload.pop("restrictedEntities", None)
        items.append(payload)
    return JsonResponse({"items": items, "productVersion": request.product_version, "canViewRestricted": request.user.is_staff})


def _segment_query(request: HttpRequest) -> tuple[date, str]:
    try:
        data_date = date.fromisoformat(str(request.GET.get("date") or ""))
    except ValueError as exc:
        raise SegmentContractError("date must use YYYY-MM-DD") from exc
    set_id = str(request.GET.get("dimensionSet") or "")
    parse_dimension_set(set_id)
    return data_date, set_id


def _legacy_segment(snapshot: InterestSnapshot | None, set_id: str) -> dict | None:
    if snapshot is None or snapshot.schema_version != "teeni-interest-snapshot/1.3.0" or set_id != "age_gender":
        return None
    demographic = snapshot.payload.get("demographicInterest")
    if not isinstance(demographic, dict):
        return None
    groups = []
    for group in demographic.get("groups", []):
        gender = {"男": "male", "女": "female"}.get(group.get("gender"))
        age = group.get("age")
        if gender is None or not isinstance(age, int):
            continue
        groups.append({
            "groupKey": f"{age}~{gender}",
            "values": {"age": age, "gender": gender},
            "groupUsers": group.get("groupUsers"),
            "eligibleUsers": group.get("eligibleUsers"),
            "sampleStatus": "可描述" if int(group.get("eligibleUsers", 0)) >= 30 else "样本不足",
            "ips": group.get("ips", []),
        })
    return {
        "available": True,
        "legacy": True,
        "schemaVersion": "teeni-interest-segments/legacy-1.3.0",
        "dataDate": snapshot.data_date.isoformat(),
        "productVersion": snapshot.product_version,
        "sceneId": PRODUCT_VERSION_SCENES[snapshot.product_version],
        "dimensionSet": set_id,
        "dimensions": ["age", "gender"],
        "sampleMinimumUsers": 30,
        "domains": {
            "ages": list(range(1, 18)),
            "genders": [{"id": "male", "label": "男"}, {"id": "female", "label": "女"}],
            "regions": [],
        },
        "overallEligibleUsers": demographic.get("overallEligibleUsers"),
        "overallIps": demographic.get("overallIps", []),
        "groups": groups,
    }


def _unavailable(data_date: date, product_version: str) -> JsonResponse:
    return JsonResponse({
        "available": False,
        "productVersion": product_version,
        "sceneId": PRODUCT_VERSION_SCENES[product_version],
        "dataDate": data_date.isoformat(),
        "detail": "该日期尚未生成多维分层。",
    })


@require_GET
@login_required
@versioned_request
def segment_summary(request: HttpRequest) -> JsonResponse:
    try:
        data_date, set_id = _segment_query(request)
    except SegmentContractError as exc:
        return _error("invalid_segment_query", str(exc))
    record = InterestSegmentSnapshot.objects.filter(product_version=request.product_version, data_date=data_date, dimension_set=set_id).first()
    if record is not None:
        payload = deepcopy(record.payload)
        payload.update({"productVersion": record.product_version, "sceneId": PRODUCT_VERSION_SCENES[record.product_version]})
        payload["available"] = True
        payload["groups"] = [
            {key: value for key, value in group.items() if key != "ipCounts"}
            | {"sampleStatus": "可描述" if int(group.get("eligibleUsers", 0)) >= 30 else "样本不足"}
            for group in payload.get("groups", [])
        ]
        return JsonResponse(payload)
    legacy = _legacy_segment(InterestSnapshot.objects.filter(product_version=request.product_version, data_date=data_date).first(), set_id)
    if legacy is None:
        return _unavailable(data_date, request.product_version)
    legacy["groups"] = [
        {key: value for key, value in group.items() if key != "ips"}
        for group in legacy["groups"]
    ]
    return JsonResponse(legacy)


@require_GET
@login_required
@versioned_request
def segment_groups(request: HttpRequest) -> JsonResponse:
    try:
        data_date, set_id = _segment_query(request)
        raw_keys = str(request.GET.get("groups") or "")
        keys = [value for value in raw_keys.split(",") if value]
        if not 1 <= len(keys) <= 4 or len(set(keys)) != len(keys):
            raise SegmentContractError("groups must contain one to four unique values")
    except SegmentContractError as exc:
        return _error("invalid_segment_query", str(exc))
    record = InterestSegmentSnapshot.objects.filter(product_version=request.product_version, data_date=data_date, dimension_set=set_id).first()
    if record is not None:
        payload = record.payload
        by_key = {group["groupKey"]: group for group in payload.get("groups", [])}
        if any(key not in by_key for key in keys):
            return _error("invalid_segment_query", "unknown segment group")
        segment_set = {"overallIps": payload["overallIps"]}
        return JsonResponse({
            "available": True,
            "productVersion": request.product_version,
            "sceneId": PRODUCT_VERSION_SCENES[request.product_version],
            "schemaVersion": payload["schemaVersion"],
            "dataDate": payload["dataDate"],
            "dimensionSet": set_id,
            "overallEligibleUsers": payload["overallEligibleUsers"],
            "overallIps": payload["overallIps"],
            "groups": [materialize_group(segment_set, by_key[key]) for key in keys],
        })
    legacy = _legacy_segment(InterestSnapshot.objects.filter(product_version=request.product_version, data_date=data_date).first(), set_id)
    if legacy is None:
        return _unavailable(data_date, request.product_version)
    by_key = {group["groupKey"]: group for group in legacy["groups"]}
    if any(key not in by_key for key in keys):
        return _error("invalid_segment_query", "unknown segment group")
    return JsonResponse({
        key: value for key, value in legacy.items()
        if key not in {"dimensions", "sampleMinimumUsers", "domains", "groups"}
    } | {"groups": [by_key[key] for key in keys]})


@require_GET
@login_required
@versioned_request
def list_candidates(request: HttpRequest) -> JsonResponse:
    if not request.user.is_staff:
        return _error("forbidden", "仅授权审核员可以查看候选审核包。", 403)
    try:
        requested_date = date.fromisoformat(request.GET["date"]) if request.GET.get("date") else None
        offset = int(request.GET.get("offset", "0"))
        limit = int(request.GET.get("limit", "100"))
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError
    except ValueError:
        return _error("invalid_candidate_query", "日期、分页偏移或分页大小无效。")
    query = InterestCandidate.objects.select_related("resolved_entity", "job").filter(job__product_version=request.product_version)
    if requested_date:
        query = query.filter(data_date=requested_date)
    count = query.count()
    candidates = list(query.order_by("-data_date", "-users", "-sessions", "normalized_phrase", "id")[offset:offset + limit])
    staging = next((
        candidate.job.runtime_state.get("interestCandidateStaging")
        for candidate in candidates
        if isinstance(candidate.job.runtime_state.get("interestCandidateStaging"), dict)
    ), None)
    batch = None
    if staging:
        batch = {
            "dates": staging.get("dates", []),
            "candidateDegraded": staging.get("candidateDegraded", False),
            "resultManifestSha256": staging.get("resultManifestSha256", ""),
        }
    return JsonResponse({
        "items": [serialize_candidate(candidate) for candidate in candidates],
        "productVersion": request.product_version,
        "canReview": request.user.is_staff,
        "batch": batch,
        "count": count,
        "nextOffset": offset + limit if offset + limit < count else None,
    })


@require_GET
@login_required
@versioned_request
def list_entities(request: HttpRequest) -> JsonResponse:
    ensure_seed_registry()
    entities = InterestEntity.objects.filter(status=InterestEntity.Status.APPROVED).select_related("parent").prefetch_related("aliases")
    if not request.user.is_staff:
        entities = entities.filter(visibility=Visibility.PUBLIC.value)
    return JsonResponse({
        "items": [serialize_entity(entity) for entity in entities],
        "canViewRestricted": request.user.is_staff,
    })


@require_POST
@login_required
@versioned_request
def review(request: HttpRequest, candidate_id) -> JsonResponse:
    if not request.user.is_staff:
        return _error("forbidden", "仅授权审核员可以修改候选。", 403)
    candidate = get_object_or_404(InterestCandidate, id=candidate_id, job__product_version=request.product_version)
    try:
        ensure_seed_registry()
        payload = _body(request)
        if payload.get("action") == "approve":
            if payload.get("entityType") not in {item.value for item in EntityType}:
                raise ValueError("invalid_entity_type")
            if payload.get("entitySubtype", EntitySubtype.OTHER_MEME.value) not in {item.value for item in EntitySubtype}:
                raise ValueError("invalid_entity_subtype")
            if payload.get("broadTopic") not in {item.value for item in BroadTopic}:
                raise ValueError("invalid_broad_topic")
            if payload.get("visibility", Visibility.PUBLIC.value) not in {item.value for item in Visibility}:
                raise ValueError("invalid_visibility")
            if payload.get("safetyCategory", SafetyCategory.NONE.value) not in {item.value for item in SafetyCategory}:
                raise ValueError("invalid_safety_category")
            if not isinstance(payload.get("aliases", []), list) or len(payload.get("aliases", [])) > 20:
                raise ValueError("invalid_aliases")
        candidate = review_candidate(candidate, request.user, payload)
    except json.JSONDecodeError:
        return _error("invalid_json", "请求正文不是有效 JSON。")
    except InterestEntity.DoesNotExist:
        return _error("target_not_found", "合并目标不存在。", 404)
    except (IntegrityError, ValidationError, ValueError) as exc:
        return _error(str(exc) if isinstance(exc, ValueError) else "duplicate_alias", "审核内容与现有实体或别名冲突。", 409)
    return JsonResponse({"candidate": serialize_candidate(candidate)})


@require_POST
@login_required
@versioned_request
def add_alias(request: HttpRequest, registry_id: str) -> JsonResponse:
    if not request.user.is_staff:
        return _error("forbidden", "仅授权审核员可以增加别名。", 403)
    entity = get_object_or_404(InterestEntity, registry_id=registry_id, status=InterestEntity.Status.APPROVED)
    try:
        payload = _body(request)
        alias = InterestAlias(entity=entity, value=str(payload.get("alias", "")))
        alias.full_clean()
        alias.save()
    except json.JSONDecodeError:
        return _error("invalid_json", "请求正文不是有效 JSON。")
    except (IntegrityError, ValidationError, ValueError):
        return _error("duplicate_alias", "别名为空或已被其他实体使用。", 409)
    entity = InterestEntity.objects.prefetch_related("aliases").get(pk=entity.pk)
    return JsonResponse({"entity": serialize_entity(entity)})

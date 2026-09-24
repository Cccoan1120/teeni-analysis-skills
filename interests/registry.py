from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from django.db import IntegrityError, transaction
from django.utils import timezone

from interest_engine.contracts import sha256_file
from interest_engine.registry import (
    BroadTopic,
    EntityRegistry,
    EntitySubtype,
    EntityType,
    MatchMode,
    MatchPolicy,
    REGISTRY_SCHEMA,
    SafetyCategory,
    Visibility,
    normalize_text,
)
from topics.models import TopicJob
from topics.storage import job_directory, safe_job_path

from .models import (
    InterestAlias,
    InterestBackfillRequest,
    InterestCandidate,
    InterestEntity,
    InterestRegistrySnapshot,
    InterestReviewEvent,
)


REGISTRY_VERSION = "db-v3"
SEED_PATH = Path(__file__).resolve().parents[1] / "interest_engine" / "resources" / "entity-registry-v1.json"


def ensure_registry(registry_path: Path) -> None:
    payload = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    registry = EntityRegistry.from_dict(payload)
    with transaction.atomic():
        by_registry_id = {}
        for order, seed in enumerate(registry.entities, start=1):
            entity = InterestEntity.objects.filter(registry_id=seed.id).first()
            if entity is None:
                entity = InterestEntity.objects.filter(
                    normalized_name=normalize_text(seed.canonical_name)
                ).first()
            if entity is None:
                entity = InterestEntity(registry_id=seed.id)
            else:
                entity.registry_id = seed.id
            entity.canonical_name = seed.canonical_name
            entity.entity_type = seed.entity_type.value
            entity.entity_subtype = seed.entity_subtype.value
            entity.broad_topic = seed.broad_topic.value
            entity.source_label = seed.source_label
            entity.source_platform = seed.source_platform
            entity.visibility = seed.visibility.value
            entity.safety_category = seed.safety_category.value
            entity.match_rules = [{
                "value": rule.value,
                "mode": rule.mode.value,
                "policy": rule.policy.value,
                **({"contextTerms": list(rule.context_terms)} if rule.context_terms else {}),
            } for rule in seed.aliases]
            entity.registry_order = order
            entity.save()
            by_registry_id[seed.id] = entity
        for seed in registry.entities:
            entity = by_registry_id[seed.id]
            parent = by_registry_id.get(seed.parent_registry_id)
            if entity.parent_id != (parent.id if parent else None):
                entity.parent = parent
                entity.save(update_fields=["parent", "updated_at"])
            for rule in seed.aliases:
                if rule.mode == MatchMode.PATTERN or normalize_text(rule.value) == entity.normalized_name:
                    continue
                alias, created = InterestAlias.objects.get_or_create(
                    normalized=normalize_text(rule.value),
                    defaults={
                        "entity": entity,
                        "value": rule.value,
                        "ambiguous": rule.policy != MatchPolicy.AUTO,
                    },
                )
                if alias.entity_id != entity.id:
                    raise IntegrityError("seed alias belongs to another entity")
                ambiguous = rule.policy != MatchPolicy.AUTO
                if not created and (alias.value != rule.value or alias.ambiguous != ambiguous):
                    alias.value = rule.value
                    alias.ambiguous = ambiguous
                    alias.save(update_fields=["value", "ambiguous"])


def ensure_seed_registry() -> None:
    ensure_registry(SEED_PATH)


def registry_payload() -> dict:
    ensure_seed_registry()
    entities = []
    for entity in InterestEntity.objects.filter(status=InterestEntity.Status.APPROVED).prefetch_related("aliases"):
        aliases = list(entity.aliases.all())
        rules = list(entity.match_rules) if isinstance(entity.match_rules, list) else []
        known_rules = {
            (str(rule.get("mode", "substring")), normalize_text(rule.get("value", "")))
            for rule in rules
            if isinstance(rule, dict)
        }
        for alias in aliases:
            key = (MatchMode.SUBSTRING.value, alias.normalized)
            if key not in known_rules:
                rules.append({
                    "value": alias.value,
                    "mode": MatchMode.SUBSTRING.value,
                    "policy": MatchPolicy.CANDIDATE.value if alias.ambiguous else MatchPolicy.AUTO.value,
                })
        item = {
            "id": entity.registry_id,
            "canonicalName": entity.canonical_name,
            "entityType": entity.entity_type,
            "entitySubtype": entity.entity_subtype,
            "broadTopic": entity.broad_topic,
            "parentRegistryId": entity.parent.registry_id if entity.parent_id else None,
            "sourceLabel": entity.source_label,
            "sourcePlatform": entity.source_platform,
            "visibility": entity.visibility,
            "safetyCategory": entity.safety_category,
            "matchRules": rules,
        }
        entities.append(item)
    result = {"schemaVersion": REGISTRY_SCHEMA, "registryVersion": REGISTRY_VERSION, "entities": entities}
    EntityRegistry.from_dict(result)
    return result


def freeze_registry(job: TopicJob) -> InterestRegistrySnapshot:
    existing = InterestRegistrySnapshot.objects.filter(job=job).first()
    if existing:
        path = safe_job_path(job.id, existing.relative_path)
        if path.is_file() and sha256_file(path) == existing.sha256:
            return existing
        raise ValueError("frozen interest registry is missing or changed")
    path = safe_job_path(job.id, "runtime/interest-1.1.0/entity-registry.json", create_parent=True)
    path.write_text(json.dumps(registry_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
    digest = sha256_file(path)
    return InterestRegistrySnapshot.objects.create(
        job=job,
        registry_version=REGISTRY_VERSION,
        sha256=digest,
        relative_path=path.resolve().relative_to(job_directory(job.id).resolve()).as_posix(),
    )


def _create_alias(entity: InterestEntity, value: str) -> InterestAlias | None:
    value = str(value).strip()
    normalized = normalize_text(value)
    if not value or normalized == entity.normalized_name:
        return None
    existing = InterestAlias.objects.filter(normalized=normalized).first()
    if existing:
        if existing.entity_id == entity.id:
            return existing
        raise IntegrityError("alias belongs to another entity")
    alias = InterestAlias(entity=entity, value=value)
    alias.full_clean()
    alias.save()
    return alias


def review_candidate(candidate: InterestCandidate, actor, payload: dict) -> InterestCandidate:
    action = str(payload.get("action", ""))
    if candidate.status != InterestCandidate.Status.PENDING:
        raise ValueError("candidate_already_reviewed")
    with transaction.atomic():
        candidate = InterestCandidate.objects.select_for_update().get(pk=candidate.pk)
        if candidate.status != InterestCandidate.Status.PENDING:
            raise ValueError("candidate_already_reviewed")
        entity = None
        if action == "reject":
            candidate.status = InterestCandidate.Status.REJECTED
        elif action == "merge":
            entity = InterestEntity.objects.get(registry_id=str(payload.get("targetRegistryId", "")), status=InterestEntity.Status.APPROVED)
            _create_alias(entity, candidate.phrase)
            candidate.status = InterestCandidate.Status.MERGED
            candidate.resolved_entity = entity
        elif action == "approve":
            canonical_name = str(payload.get("canonicalName", "")).strip()
            entity_type = str(payload.get("entityType", ""))
            broad_topic = str(payload.get("broadTopic", ""))
            entity_subtype = str(payload.get("entitySubtype") or EntitySubtype.OTHER_MEME.value)
            parent_registry_id = str(payload.get("parentRegistryId") or "").strip()
            visibility = str(payload.get("visibility") or Visibility.PUBLIC.value)
            safety_category = str(payload.get("safetyCategory") or SafetyCategory.NONE.value)
            if (
                not canonical_name
                or entity_type not in {item.value for item in EntityType}
                or entity_subtype not in {item.value for item in EntitySubtype}
                or broad_topic not in {item.value for item in BroadTopic}
                or visibility not in {item.value for item in Visibility}
                or safety_category not in {item.value for item in SafetyCategory}
                or not isinstance(payload.get("aliases", []), list)
            ):
                raise ValueError("invalid_entity")
            parent = None
            if parent_registry_id:
                parent = InterestEntity.objects.get(
                    registry_id=parent_registry_id,
                    status=InterestEntity.Status.APPROVED,
                )
            entity = InterestEntity(
                canonical_name=canonical_name,
                entity_type=entity_type,
                entity_subtype=entity_subtype,
                broad_topic=broad_topic,
                parent=parent,
                source_label="候选人工审核",
                visibility=visibility,
                safety_category=safety_category,
                match_rules=[{"value": canonical_name, "mode": "substring", "policy": "auto"}],
            )
            entity.full_clean()
            entity.save()
            aliases = [candidate.phrase, *payload.get("aliases", [])]
            for alias in dict.fromkeys(str(value).strip() for value in aliases if str(value).strip()):
                _create_alias(entity, alias)
            candidate.status = InterestCandidate.Status.APPROVED
            candidate.resolved_entity = entity
        else:
            raise ValueError("invalid_action")
        candidate.reviewed_by = actor
        candidate.reviewed_at = timezone.now()
        candidate.save(update_fields=["status", "resolved_entity", "reviewed_by", "reviewed_at"])
        InterestReviewEvent.objects.create(
            candidate=candidate,
            actor=actor,
            action=action,
            details={"entityRegistryId": entity.registry_id if entity else None},
        )
        if entity:
            InterestBackfillRequest.objects.create(
                entity=entity,
                product_version=candidate.job.product_version,
                start_date=candidate.data_date - timedelta(days=7),
                end_date=candidate.data_date,
                requested_by=actor,
            )
    return candidate

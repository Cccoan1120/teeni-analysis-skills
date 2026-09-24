from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


REGISTRY_SCHEMA = "teeni-interest-registry/1.1.0"
LEGACY_REGISTRY_SCHEMA = "teeni-interest-registry/1.0.0"


class EntityType(StrEnum):
    WORK = "作品"
    CHARACTER = "角色"
    GAME = "游戏"
    TOY_BRAND = "玩具/品牌"
    CREATOR = "创作者/账号"
    MEME = "网络热梗"
    OTHER = "其他"


class BroadTopic(StrEnum):
    SCREEN_CHARACTERS = "影视动漫与角色"
    VIDEO_GAMES = "电子游戏与互动玩法"
    STORIES = "故事绘本与文学"
    MUSIC = "音乐儿歌与音频"
    LEARNING = "学习语言与数学"
    KNOWLEDGE = "科学自然与百科"
    ANIMALS = "动物植物与宠物"
    RELATIONSHIPS = "家庭校园与人际"
    EMOTIONS = "情绪陪伴与自我表达"
    DAILY_LIFE = "日常生活与饮食健康"
    DEVICES = "设备与产品使用"
    OTHER = "其他明确内容"


class EntitySubtype(StrEnum):
    ANIMATION = "动画"
    TOKUSATSU = "特摄"
    FILM = "电影"
    TV_SERIES = "电视剧"
    LITERATURE = "文学绘本"
    MUSIC_AUDIO = "音乐音频"
    MOBILE_GAME = "手游"
    PC_GAME = "端游"
    CONSOLE_GAME = "主机游戏"
    SANDBOX_UGC = "沙盒 UGC"
    WORK_CHARACTER = "作品角色"
    CHARACTER_GROUP = "角色组合"
    ART_TOY = "潮玩"
    TOY = "玩具"
    STREAMER = "主播"
    SHORT_VIDEO_ACCOUNT = "短视频账号"
    SCREEN_QUOTE = "影视台词梗"
    GAME_VOICE = "游戏语音梗"
    STREAMER_CLIP = "主播切片"
    MONDEGREEN_AUDIO = "空耳音频"
    INTERNET_PHRASE = "网络句式"
    ABSTRACT_FOOD = "抽象食品"
    VULGAR_SLANG = "低俗黑话"
    DISCRIMINATORY = "歧视性用语"
    RISK_EVENT = "风险事件"
    OTHER_MEME = "其他热梗"


class Visibility(StrEnum):
    PUBLIC = "public"
    RESTRICTED = "restricted"


class SafetyCategory(StrEnum):
    NONE = "none"
    SEXUAL_CONTENT = "sexual_content"
    DISCRIMINATION = "discrimination"
    REAL_CASE = "real_case"
    EATING_DISORDER = "eating_disorder"
    INSULT = "insult"


class MatchMode(StrEnum):
    SUBSTRING = "substring"
    EXACT = "exact"
    TOKEN = "token"
    PATTERN = "pattern"


class MatchPolicy(StrEnum):
    AUTO = "auto"
    CONTEXT = "context"
    CANDIDATE = "candidate"


STANDALONE_IP_SUBTYPES = {
    EntityType.WORK: frozenset({
        EntitySubtype.ANIMATION,
        EntitySubtype.TOKUSATSU,
        EntitySubtype.FILM,
        EntitySubtype.TV_SERIES,
        EntitySubtype.LITERATURE,
        EntitySubtype.MUSIC_AUDIO,
    }),
    EntityType.CHARACTER: frozenset({EntitySubtype.WORK_CHARACTER, EntitySubtype.CHARACTER_GROUP}),
    EntityType.GAME: frozenset({
        EntitySubtype.MOBILE_GAME,
        EntitySubtype.PC_GAME,
        EntitySubtype.CONSOLE_GAME,
        EntitySubtype.SANDBOX_UGC,
    }),
    EntityType.TOY_BRAND: frozenset({EntitySubtype.ART_TOY, EntitySubtype.TOY}),
    EntityType.CREATOR: frozenset({EntitySubtype.STREAMER, EntitySubtype.SHORT_VIDEO_ACCOUNT}),
}


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s，。！？、,.!?；;：:“”\"'‘’（）()《》【】\[\]~～_\-]+", "", normalized)


@dataclass(frozen=True)
class EntityAlias:
    value: str
    normalized: str
    mode: MatchMode = MatchMode.SUBSTRING
    policy: MatchPolicy = MatchPolicy.AUTO
    context_terms: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return self.policy != MatchPolicy.AUTO


@dataclass(frozen=True)
class Entity:
    id: str
    canonical_name: str
    entity_type: EntityType
    broad_topic: BroadTopic
    aliases: tuple[EntityAlias, ...]
    entity_subtype: EntitySubtype
    parent_registry_id: str | None
    source_label: str
    source_platform: str
    visibility: Visibility
    safety_category: SafetyCategory


def is_standalone_ip(entity: Entity) -> bool:
    return (
        entity.parent_registry_id is None
        and entity.entity_subtype in STANDALONE_IP_SUBTYPES.get(entity.entity_type, frozenset())
    )


@dataclass(frozen=True)
class EntityMatch:
    entity: Entity
    alias: EntityAlias


class EntityRegistry:
    def __init__(self, *, version: str, entities: tuple[Entity, ...]):
        self.version = version
        self.entities = entities
        aliases: list[tuple[EntityAlias, Entity]] = []
        for entity in entities:
            aliases.extend((alias, entity) for alias in entity.aliases)
        self._aliases = tuple(sorted(
            aliases,
            key=lambda item: (item[0].mode == MatchMode.PATTERN, len(item[0].normalized)),
            reverse=True,
        ))
        self._by_id = {entity.id: entity for entity in entities}

    @staticmethod
    def _legacy_subtype(entity_id: str, entity_type: EntityType) -> EntitySubtype:
        if entity_id in {"IE0001", "IE0064", "IE0072"}:
            return EntitySubtype.TOKUSATSU
        if entity_id in {"IE0020", "IE0037", "IE0038", "IE0039", "IE0075"}:
            return EntitySubtype.FILM
        if entity_id == "IE0069":
            return EntitySubtype.LITERATURE
        if entity_id in {"IE0044", "IE0045", "IE0055", "IE0059"}:
            return EntitySubtype.SANDBOX_UGC
        if entity_id in {"IE0056", "IE0057", "IE0058"}:
            return EntitySubtype.CONSOLE_GAME
        if entity_type == EntityType.GAME:
            return EntitySubtype.MOBILE_GAME
        if entity_type == EntityType.CHARACTER:
            return EntitySubtype.WORK_CHARACTER
        if entity_type == EntityType.TOY_BRAND:
            return EntitySubtype.ART_TOY if entity_id == "IE0067" else EntitySubtype.TOY
        if entity_type == EntityType.CREATOR:
            return EntitySubtype.SHORT_VIDEO_ACCOUNT
        if entity_type == EntityType.MEME:
            return EntitySubtype.OTHER_MEME
        return EntitySubtype.ANIMATION if entity_type == EntityType.WORK else EntitySubtype.OTHER_MEME

    @staticmethod
    def _validate_pattern(value: str) -> None:
        if len(value) > 128 or not value.startswith("^") or not value.endswith("$") or ".*" in value:
            raise ValueError("pattern rules must be anchored and length-bounded")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError("invalid pattern rule") from exc

    @classmethod
    def from_dict(cls, payload: dict) -> "EntityRegistry":
        schema = payload.get("schemaVersion")
        if schema not in {REGISTRY_SCHEMA, LEGACY_REGISTRY_SCHEMA}:
            raise ValueError("unsupported entity registry schema")
        seen_ids: set[str] = set()
        seen_aliases: dict[tuple[MatchMode, str], str] = {}
        seen_names: set[str] = set()
        entities: list[Entity] = []
        for item in payload.get("entities", []):
            entity_id = str(item.get("id", "")).strip()
            if not entity_id or entity_id in seen_ids:
                raise ValueError("entity ids must be non-empty and unique")
            seen_ids.add(entity_id)
            canonical = str(item.get("canonicalName", "")).strip()
            normalized_name = normalize_text(canonical)
            if not normalized_name or normalized_name in seen_names:
                raise ValueError("entity canonical names must be non-empty and unique")
            seen_names.add(normalized_name)
            aliases: list[EntityAlias] = []
            rules = item.get("matchRules")
            if rules is None:
                rules = [
                    {"value": value, "mode": "substring", "policy": "auto"}
                    for value in [canonical, *(item.get("aliases") or [])]
                ] + [
                    {"value": value, "mode": "substring", "policy": "candidate"}
                    for value in item.get("ambiguousAliases") or []
                ]
            if not isinstance(rules, list):
                raise ValueError("matchRules must be a list")
            for raw_rule in rules:
                if not isinstance(raw_rule, dict):
                    raise ValueError("matchRules entries must be objects")
                value = str(raw_rule.get("value", "")).strip()
                mode = MatchMode(raw_rule.get("mode", MatchMode.SUBSTRING.value))
                policy = MatchPolicy(raw_rule.get("policy", MatchPolicy.AUTO.value))
                normalized = value.casefold() if mode == MatchMode.PATTERN else normalize_text(value)
                if mode == MatchMode.PATTERN:
                    cls._validate_pattern(value)
                if not normalized or any(existing.mode == mode and existing.normalized == normalized for existing in aliases):
                    continue
                owner = seen_aliases.get((mode, normalized))
                if owner and owner != entity_id:
                    raise ValueError(f"alias belongs to multiple entities: {value}")
                seen_aliases[(mode, normalized)] = entity_id
                raw_context = raw_rule.get("contextTerms") or []
                if not isinstance(raw_context, list):
                    raise ValueError("contextTerms must be a list")
                context_terms = tuple(term for term in (normalize_text(value) for value in raw_context) if term)
                if policy == MatchPolicy.CONTEXT and not context_terms:
                    raise ValueError("context rules require contextTerms")
                aliases.append(EntityAlias(value, normalized, mode, policy, context_terms))
            if not canonical or not aliases:
                raise ValueError("entity canonical name and aliases are required")
            entity_type = EntityType(item["entityType"])
            entities.append(Entity(
                id=entity_id,
                canonical_name=canonical,
                entity_type=entity_type,
                broad_topic=BroadTopic(item["broadTopic"]),
                aliases=tuple(aliases),
                entity_subtype=EntitySubtype(
                    item.get("entitySubtype") or cls._legacy_subtype(entity_id, entity_type).value
                ),
                parent_registry_id=str(item.get("parentRegistryId") or "").strip() or None,
                source_label=str(item.get("sourceLabel") or "初始实体种子").strip(),
                source_platform=str(item.get("sourcePlatform") or "").strip(),
                visibility=Visibility(item.get("visibility", Visibility.PUBLIC.value)),
                safety_category=SafetyCategory(item.get("safetyCategory", SafetyCategory.NONE.value)),
            ))
        by_id = {entity.id: entity for entity in entities}
        for entity in entities:
            depth = 0
            visited = {entity.id}
            parent_id = entity.parent_registry_id
            while parent_id:
                if parent_id not in by_id:
                    raise ValueError(f"unknown parent entity: {parent_id}")
                if parent_id in visited:
                    raise ValueError("entity parent hierarchy must be acyclic")
                visited.add(parent_id)
                depth += 1
                if depth > 2:
                    raise ValueError("entity parent hierarchy may contain at most three levels")
                parent_id = by_id[parent_id].parent_registry_id
        return cls(version=str(payload.get("registryVersion", "")), entities=tuple(entities))

    @classmethod
    def from_json(cls, path: str | Path) -> "EntityRegistry":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8-sig")))

    def match(self, text: str) -> tuple[EntityMatch, ...]:
        normalized = normalize_text(text)
        token_text = unicodedata.normalize("NFKC", str(text or "")).casefold()
        matches: list[EntityMatch] = []
        matched_ids: set[str] = set()
        occupied: list[tuple[int, int]] = []
        for alias, entity in self._aliases:
            if alias.policy == MatchPolicy.CANDIDATE or entity.id in matched_ids:
                continue
            if alias.policy == MatchPolicy.CONTEXT and not any(term in normalized for term in alias.context_terms):
                continue
            if alias.mode == MatchMode.EXACT:
                start = 0 if normalized == alias.normalized else -1
                end = len(normalized)
            elif alias.mode == MatchMode.TOKEN:
                token_match = re.search(rf"(?<![\w]){re.escape(alias.value.casefold())}(?![\w])", token_text)
                start = token_match.start() if token_match else -1
                end = token_match.end() if token_match else -1
            elif alias.mode == MatchMode.PATTERN:
                pattern_match = re.fullmatch(alias.value, normalized, flags=re.IGNORECASE)
                start = pattern_match.start() if pattern_match else -1
                end = pattern_match.end() if pattern_match else -1
            else:
                start = normalized.find(alias.normalized)
                end = start + len(alias.normalized) if start >= 0 else -1
            if start < 0:
                continue
            if any(start < taken_end and end > taken_start for taken_start, taken_end in occupied):
                continue
            occupied.append((start, end))
            matched_ids.add(entity.id)
            matches.append(EntityMatch(entity, alias))
        return tuple(matches)

    def get(self, entity_id: str) -> Entity:
        return self._by_id[entity_id]

    def ancestors(self, entity_id: str) -> tuple[Entity, ...]:
        ancestors: list[Entity] = []
        parent_id = self._by_id[entity_id].parent_registry_id
        while parent_id:
            parent = self._by_id[parent_id]
            ancestors.append(parent)
            parent_id = parent.parent_registry_id
        return tuple(ancestors)

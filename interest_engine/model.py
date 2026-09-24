from __future__ import annotations

import json
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.request import Request, urlopen

from .registry import EntitySubtype, EntityType


class CandidateEnricher(Protocol):
    def enrich(self, candidates: list[dict]) -> list[dict]: ...


class LocalCandidatePassthroughEnricher:
    def enrich(self, candidates: list[dict]) -> list[dict]:
        return [
            {
                **item,
                "accepted": False,
                "suggestedName": item["phrase"],
                "suggestedType": EntityType.OTHER.value,
                "suggestedSubtype": EntitySubtype.OTHER_MEME.value,
                "suggestedParentName": "",
                "suggestedAliases": [],
                "modelConfidence": "未运行",
            }
            for item in candidates
        ]


@dataclass
class QwenCandidateEnricher:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 120

    @classmethod
    def from_environment(cls) -> "QwenCandidateEnricher":
        api_key = os.environ.get("TEENI_TOPICS_API_KEY", "")
        if not api_key and os.environ.get("TEENI_TOPICS_API_KEY_FILE"):
            api_key = Path(os.environ["TEENI_TOPICS_API_KEY_FILE"]).read_text(encoding="utf-8").strip()
        if not api_key:
            raise ValueError("Qwen candidate API key is not configured")
        return cls(
            base_url=os.environ["TEENI_TOPICS_BASE_URL"],
            api_key=api_key,
            model=os.environ.get("TEENI_TOPIC_MODEL", "Qwen3.8-27B"),
        )

    def _endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def enrich(self, candidates: list[dict]) -> list[dict]:
        enriched: list[dict] = []
        allowed_types = {item.value for item in EntityType}
        allowed_subtypes = {item.value for item in EntitySubtype}
        for start in range(0, min(len(candidates), 200), 20):
            batch = candidates[start:start + 20]
            items = [{
                "id": f"C{index + 1:03d}",
                "phrase": item["phrase"],
                "users": item["users"],
                "sessions": item["sessions"],
                "examples": item.get("examples", [])[:3],
            } for index, item in enumerate(batch)]
            type_values = "、".join(item.value for item in EntityType)
            subtype_values = "、".join(item.value for item in EntitySubtype)
            prompt = (
                "你在审核儿童AI对话中的热门实体候选。只判断候选是否是作品、角色、游戏、玩具/品牌、"
                "创作者/账号、网络热梗或其他可稳定追踪实体。不得创造不存在的名称。"
                "模型只建议规范名、实体类型、固定子类型、父IP名称和别名，不直接批准入库。"
                f"实体类型只能是：{type_values}。固定子类型只能是：{subtype_values}。"
                "返回JSON对象，格式为{\"items\":[{\"id\":\"C001\",\"accepted\":true,"
                "\"canonicalName\":\"...\",\"entityType\":\"网络热梗\",\"entitySubtype\":\"网络句式\","
                "\"parentName\":\"\",\"aliases\":[],\"confidence\":\"高\"}]}。\n"
                + json.dumps(items, ensure_ascii=False)
            )
            body = json.dumps({
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }, ensure_ascii=False).encode("utf-8")
            request = Request(
                self._endpoint(),
                data=body,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
            with urlopen(request, timeout=self.timeout_seconds, context=context) as response:
                payload = json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            by_id = {str(item.get("id")): item for item in parsed.get("items", [])}
            for index, source in enumerate(batch):
                result = by_id.get(f"C{index + 1:03d}", {})
                entity_type = result.get("entityType") if result.get("entityType") in allowed_types else EntityType.OTHER.value
                enriched.append({
                    **source,
                    "accepted": bool(result.get("accepted")),
                    "suggestedName": str(result.get("canonicalName") or source["phrase"])[:80],
                    "suggestedType": entity_type,
                    "suggestedSubtype": result.get("entitySubtype") if result.get("entitySubtype") in allowed_subtypes else EntitySubtype.OTHER_MEME.value,
                    "suggestedParentName": str(result.get("parentName") or "")[:80],
                    "suggestedAliases": [str(value)[:80] for value in result.get("aliases", [])[:10]],
                    "modelConfidence": result.get("confidence") if result.get("confidence") in {"高", "中", "低"} else "低",
                })
        return enriched


def enrich_candidates(candidates: list[dict], enricher: CandidateEnricher | None) -> tuple[list[dict], bool, str | None]:
    if not candidates:
        return [], False, None
    if enricher is None:
        return [
            {**item, "accepted": False, "suggestedName": item["phrase"], "suggestedType": EntityType.OTHER.value,
             "suggestedSubtype": EntitySubtype.OTHER_MEME.value, "suggestedParentName": "",
             "suggestedAliases": [], "modelConfidence": "未运行"}
            for item in candidates
        ], True, "candidate_model_not_configured"
    try:
        return enricher.enrich(candidates), False, None
    except Exception:
        return [
            {**item, "accepted": False, "suggestedName": item["phrase"], "suggestedType": EntityType.OTHER.value,
             "suggestedSubtype": EntitySubtype.OTHER_MEME.value, "suggestedParentName": "",
             "suggestedAliases": [], "modelConfidence": "失败"}
            for item in candidates
        ], True, "candidate_model_unavailable"

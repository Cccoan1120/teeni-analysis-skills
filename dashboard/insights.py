from __future__ import annotations

from datetime import date
import hashlib
import json
from typing import Any

from django.db import transaction
from django.utils import timezone

from publisher.privacy import AggregatePrivacyError, assert_management_insight_text

from .models import DailySnapshot, ManagementInsight, ManagementInsightRevision
from .product_versions import DEFAULT_PRODUCT_VERSION


class InsightValidationError(ValueError):
    pass


class InsightConflictError(ValueError):
    pass


FACT_STATUSES = {"confirmed", "association", "hypothesis"}
CONTENT_LIST_FIELDS = ("mechanisms", "associations", "hypotheses", "actions")
STRUCTURE_VERSION = "teeni-session-structure/1.0.0"
METRICS_VERSION = "teeni-management-insight-metrics/1.2.0"


def _percent(value: float, digits: int = 2) -> str:
    return f"{value * 100:.{digits}f}%"


def _signed_percent(value: float, digits: int = 2) -> str:
    return f"{'+' if value >= 0 else ''}{value * 100:.{digits}f}%"


def _signed_points(value: float, digits: int = 2) -> str:
    return f"{'+' if value >= 0 else ''}{value * 100:.{digits}f} 个百分点"


def _signed_number(value: float, suffix: str = "", digits: int = 2) -> str:
    return f"{'+' if value >= 0 else ''}{value:.{digits}f}{suffix}"


def _snapshot_contract(snapshot: DailySnapshot) -> tuple[str, str, str | None]:
    return (
        snapshot.product_version,
        snapshot.scene_id,
        snapshot.payload.get('metrics', {}).get('sessionStructure', {}).get('schemaVersion'),
    )


def _point(snapshot: DailySnapshot) -> dict[str, Any]:
    metrics = snapshot.payload["metrics"]
    structure = metrics.get('sessionStructure')
    if not structure or structure.get('schemaVersion') != STRUCTURE_VERSION:
        raise InsightValidationError("所选日期的会话结构指标待补齐。")
    return {
        "date": snapshot.data_date.isoformat(),
        "turns": structure['totalTurns'],
        "sessions": structure['totalSessions'],
        "users": metrics["volume"]["users"],
        **{key: structure[key] for key in ('multiTurnSessions', 'multiTurnTurns', 'fivePlusSessions')},
        **{key: structure[key] for key in ('averageTurns', 'multiTurnRate', 'multiTurnAverageTurns', 'fivePlusRate')},
    }


def _comparison_facts(start: dict[str, Any], end: dict[str, Any]) -> list[dict[str, str]]:
    facts = []
    for key, label in (('turns', '总轮次'), ('sessions', '会话数'), ('users', '用户数')):
        a, b = start[key], end[key]
        facts.append({'label': label, 'value': _signed_percent((b-a)/a) if a else '无基期样本',
                      'note': f'{a:,} 变为 {b:,}，变化 {b-a:+,}。', 'status': 'confirmed'})
    for key, label in (('averageTurns', '平均会话轮次'), ('multiTurnRate', '多轮会话率'),
                       ('multiTurnAverageTurns', '多轮会话平均轮次'), ('fivePlusRate', '5 轮及以上会话占比')):
        a, b = start[key], end[key]
        is_rate = key.endswith('Rate')
        available = a is not None and b is not None
        denominator = 'multiTurnSessions' if key == 'multiTurnAverageTurns' else 'sessions'
        numerator = {'averageTurns': 'turns', 'multiTurnRate': 'multiTurnSessions',
                     'multiTurnAverageTurns': 'multiTurnTurns', 'fivePlusRate': 'fivePlusSessions'}[key]
        fraction = f"{start[numerator]:,}/{start[denominator]:,} 变为 {end[numerator]:,}/{end[denominator]:,}"
        facts.append({'label': label,
                      'value': (_signed_points(b-a) if is_rate else _signed_number(b-a, ' 轮')) if available else '无样本',
                      'note': (f'{_percent(a)} 变为 {_percent(b)}；{fraction}。' if is_rate else f'{a:.2f} 轮变为 {b:.2f} 轮；{fraction}。') if available else f'{fraction}；任一日期无分母，不计算变化。',
                      'status': 'confirmed'})
    return facts


def _shapley(start: dict[str, Any], end: dict[str, Any]) -> dict[str, Any]:
    available = start['averageTurns'] is not None and end['averageTurns'] is not None
    session_contribution = (end["sessions"] - start["sessions"]) * (start['averageTurns'] + end['averageTurns']) / 2 if available else None
    depth_contribution = (end['averageTurns'] - start['averageTurns']) * (start["sessions"] + end["sessions"]) / 2 if available else None
    total_delta = end["turns"] - start["turns"]
    return {
        "totalDelta": round(total_delta),
        "sessions": round(session_contribution, 6) if available else None,
        "depth": round(depth_contribution, 6) if available else None,
        "sessionsShare": session_contribution / total_delta if available and total_delta else None,
        "depthShare": depth_contribution / total_delta if available and total_delta else None,
    }


def _validate_fact(value: Any, index: int) -> dict[str, str]:
    if not isinstance(value, dict):
        raise InsightValidationError(f"第 {index + 1} 条关键证据格式无效。")
    status = value.get("status")
    if status not in FACT_STATUSES:
        raise InsightValidationError(f"第 {index + 1} 条关键证据状态无效。")
    result = {"status": status}
    for key, limit in (("label", 60), ("value", 80), ("note", 360)):
        text = str(value.get(key, "")).strip()
        try:
            assert_management_insight_text(text, f"facts[{index}].{key}", limit)
        except AggregatePrivacyError as exc:
            raise InsightValidationError(str(exc)) from exc
        result[key] = text
    return result


def validate_content(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InsightValidationError("说明内容必须是对象。")
    result: dict[str, Any] = {}
    for key, limit in (("title", 140), ("summary", 1200)):
        value = str(payload.get(key, "")).strip()
        try:
            assert_management_insight_text(value, key, limit)
        except AggregatePrivacyError as exc:
            raise InsightValidationError(str(exc)) from exc
        result[key] = value

    facts = payload.get("facts")
    if not isinstance(facts, list) or not 1 <= len(facts) <= 16:
        raise InsightValidationError("关键证据需保留 1 至 16 条。")
    result["facts"] = [_validate_fact(value, index) for index, value in enumerate(facts)]

    for field in CONTENT_LIST_FIELDS:
        values = payload.get(field, [])
        if not isinstance(values, list) or len(values) > 12:
            raise InsightValidationError(f"{field} 最多允许 12 条。")
        cleaned = []
        for index, value in enumerate(values):
            text = str(value).strip()
            try:
                assert_management_insight_text(text, f"{field}[{index}]", 360)
            except AggregatePrivacyError as exc:
                raise InsightValidationError(str(exc)) from exc
            cleaned.append(text)
        result[field] = cleaned
    return result


def generated_content(
    start_date: date,
    end_date: date,
    product_version: str = DEFAULT_PRODUCT_VERSION,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    snapshots = list(
        DailySnapshot.objects.filter(
            product_version=product_version,
            data_date__range=(start_date, end_date),
        ).order_by("data_date")
    )
    if not snapshots or snapshots[0].data_date != start_date or snapshots[-1].data_date != end_date:
        raise InsightValidationError("起止日期必须都有已发布的聚合快照。")
    contract = _snapshot_contract(snapshots[0])
    if any(_snapshot_contract(item) != contract for item in snapshots[1:]):
        raise InsightValidationError("所选日期跨越会话结构定义，不能生成连续趋势说明。")

    points = [_point(item) for item in snapshots]
    comparison_start = points[0]
    comparison_label = f"{start_date.isoformat()} 至 {end_date.isoformat()}"
    if start_date == end_date:
        previous = (
            DailySnapshot.objects.filter(
                product_version=product_version,
                data_date__lt=start_date,
                payload__metrics__sessionStructure__schemaVersion=STRUCTURE_VERSION,
                payload__metrics__sessionStructure__totalSessions__gt=0,
            )
            .order_by("-data_date")
            .first()
        )
        if previous and _snapshot_contract(previous) == contract:
            comparison_start = _point(previous)
            comparison_label = f"较 {previous.data_date.isoformat()}"

    comparison_end = points[-1]
    comparison_available = comparison_start is not comparison_end and comparison_start['averageTurns'] is not None and comparison_end['averageTurns'] is not None
    facts = _comparison_facts(comparison_start, comparison_end)
    contributions = _shapley(comparison_start, comparison_end)
    total_change = (comparison_end["turns"] - comparison_start["turns"]) / comparison_start["turns"] if comparison_start['turns'] else 0
    direction = "下降" if total_change < 0 else "上升" if total_change > 0 else "持平"
    depth_share = contributions["depthShare"] or 0
    depth_explanation = (
        f"平均深度贡献 {(contributions['depth'] or 0):+,.0f} 轮，会话数贡献 {(contributions['sessions'] or 0):+,.0f} 轮；总变化为零，不计算贡献占比。"
        if total_change == 0 else
        f"平均深度变化{'抵消' if depth_share < 0 else '贡献'}了净轮次变化幅度的 {abs(depth_share) * 100:.2f}%，有符号贡献占比为 {depth_share * 100:+.2f}%。"
    )
    summary = (
        f"{comparison_label}，总轮次{direction} {abs(total_change) * 100:.2f}%。"
        f"按总轮次等于会话数乘以平均会话轮次的对称分解，{depth_explanation}"
    )
    mechanisms = [
        f"总轮次变化可由会话数与平均会话轮次的乘法恒等式分解；平均深度贡献 {(contributions['depth'] or 0):+,.0f} 轮，会话数贡献 {(contributions['sessions'] or 0):+,.0f} 轮。"
    ]
    associations = [
        "会话轮次反映当天可见记录的使用深度，不单独证明需求满足或效果改善。"
    ]
    content = validate_content(
        {
            "title": f"{start_date.isoformat()}" if start_date == end_date else f"{start_date.isoformat()} 至 {end_date.isoformat()} 趋势说明",
            "summary": summary,
            "facts": facts,
            "mechanisms": mechanisms,
            "associations": associations,
            "hypotheses": ["待验证是否存在服务可用性、时段策略或自然日期结构变化。"],
            "actions": ["核对异常时段的服务日志和运营动作，再决定是否形成原因结论。"],
        }
    )
    if not comparison_available:
        content.update(
            summary=f"{end_date.isoformat()} 共 {comparison_end['turns']:,} 轮、{comparison_end['sessions']:,} 个会话、{comparison_end['users']:,} 位用户。暂无同定义且有样本的可比日期，不计算变化率或轮次贡献。",
            facts=[{"status": "confirmed", "label": label, "value": f"{comparison_end[key]:,}",
                    "note": "所选日期的聚合计数，暂无同定义且有样本的可比日期。"}
                   for label, key in [("总轮次", "turns"), ("会话数", "sessions"), ("用户数", "users")]],
            mechanisms=[], associations=[], hypotheses=[], actions=["积累同定义日期后再比较变化。"],
        )
    metric_snapshot = {
        "schemaVersion": METRICS_VERSION,
        "comparisonLabel": comparison_label,
        "comparisonAvailable": comparison_available,
        "contract": {
            "productVersion": contract[0],
            "sceneId": contract[1],
            "sessionStructureVersion": contract[2],
        },
        "points": points,
        "contributions": contributions,
    }
    source_snapshots = snapshots + ([previous] if start_date == end_date and comparison_available else [])
    source_hashes = {item.data_date.isoformat(): _source_hash(item) for item in source_snapshots}
    return content, metric_snapshot, source_hashes


def _source_hash(snapshot):
    binding = {'workbookSha256': snapshot.workbook_sha256, 'point': _point(snapshot),
               'sessionStructure': snapshot.payload['metrics']['sessionStructure']}
    return hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@transaction.atomic
def create_generated_insight(
    start_date: date,
    end_date: date,
    actor,
    origin: str = ManagementInsight.Origin.MANUAL,
    product_version: str = DEFAULT_PRODUCT_VERSION,
) -> ManagementInsight:
    if end_date < start_date:
        raise InsightValidationError("结束日期不能早于开始日期。")
    content, metric_snapshot, source_hashes = generated_content(
        start_date,
        end_date,
        product_version,
    )
    insight = ManagementInsight.objects.create(
        product_version=product_version,
        start_date=start_date,
        end_date=end_date,
        origin=origin,
        created_by=actor,
        current_revision=1,
    )
    ManagementInsightRevision.objects.create(
        insight=insight,
        revision=1,
        created_by=actor,
        metric_snapshot=metric_snapshot,
        source_hashes=source_hashes,
        **content,
    )
    return insight


@transaction.atomic
def ensure_daily_draft(snapshot: DailySnapshot) -> ManagementInsight | None:
    data_date = snapshot.data_date if isinstance(snapshot.data_date, date) else date.fromisoformat(str(snapshot.data_date))
    existing = ManagementInsight.objects.select_for_update().filter(
        product_version=snapshot.product_version,
        start_date=data_date,
        end_date=data_date,
        origin=ManagementInsight.Origin.AUTOMATIC,
    ).first()
    if _snapshot_contract(snapshot)[2] != STRUCTURE_VERSION:
        return existing
    if existing:
        current = existing.revisions.get(revision=existing.current_revision)
        if current.created_by_id is not None:
            return existing
        content, metrics, hashes = generated_content(data_date, data_date, snapshot.product_version)
        if current.metric_snapshot != metrics or current.source_hashes != hashes:
            revision = existing.current_revision + 1
            ManagementInsightRevision.objects.create(insight=existing, revision=revision,
                metric_snapshot=metrics, source_hashes=hashes, created_by=None, **content)
            existing.current_revision = revision
            existing.save(update_fields=['current_revision', 'updated_at'])
        return existing
    return create_generated_insight(
        data_date,
        data_date,
        actor=None,
        origin=ManagementInsight.Origin.AUTOMATIC,
        product_version=snapshot.product_version,
    )


@transaction.atomic
def save_revision(insight_id, payload: dict[str, Any], actor) -> ManagementInsight:
    insight = ManagementInsight.objects.select_for_update().get(id=insight_id)
    try:
        base_revision = int(payload.get("baseRevision"))
    except (TypeError, ValueError) as exc:
        raise InsightValidationError("baseRevision 无效。") from exc
    if base_revision != insight.current_revision:
        raise InsightConflictError("该说明已被其他成员更新，请刷新后再编辑。")
    content = validate_content(payload)
    previous = insight.revisions.get(revision=insight.current_revision)
    next_revision = insight.current_revision + 1
    ManagementInsightRevision.objects.create(
        insight=insight,
        revision=next_revision,
        created_by=actor,
        metric_snapshot=previous.metric_snapshot,
        source_hashes=previous.source_hashes,
        **content,
    )
    insight.current_revision = next_revision
    insight.status = ManagementInsight.Status.DRAFT if insight.status == ManagementInsight.Status.WITHDRAWN else insight.status
    insight.save(update_fields=["current_revision", "status", "updated_at"])
    return insight


@transaction.atomic
def publish_revision(insight_id, revision: int, actor) -> ManagementInsight:
    insight = ManagementInsight.objects.select_for_update().get(id=insight_id)
    if not insight.revisions.filter(revision=revision).exists():
        raise InsightValidationError("指定版本不存在。")
    insight.status = ManagementInsight.Status.PUBLISHED
    insight.published_revision = revision
    insight.published_by = actor
    insight.published_at = timezone.now()
    insight.save(update_fields=["status", "published_revision", "published_by", "published_at", "updated_at"])
    return insight


@transaction.atomic
def withdraw_insight(insight_id, actor) -> ManagementInsight:
    insight = ManagementInsight.objects.select_for_update().get(id=insight_id)
    if insight.status != ManagementInsight.Status.PUBLISHED:
        raise InsightValidationError("只有已发布说明可以撤回。")
    insight.status = ManagementInsight.Status.WITHDRAWN
    insight.published_by = actor
    insight.save(update_fields=["status", "published_by", "updated_at"])
    return insight


def source_is_stale(revision: ManagementInsightRevision) -> bool:
    structural = revision.metric_snapshot.get('contract', {}).get('sessionStructureVersion') == STRUCTURE_VERSION
    current = {
        item.data_date.isoformat(): (_source_hash(item) if _snapshot_contract(item)[2] == STRUCTURE_VERSION else None) if structural else item.workbook_sha256
        for item in DailySnapshot.objects.filter(
            product_version=revision.insight.product_version,
            data_date__in=revision.source_hashes.keys(),
        )
    }
    return current != revision.source_hashes


def _actor_label(actor) -> str:
    if actor is None:
        return "系统"
    return actor.email or actor.username


def serialize_revision(revision: ManagementInsightRevision, include_content: bool = True) -> dict[str, Any]:
    result = {
        "revision": revision.revision,
        "createdAt": revision.created_at.isoformat(),
        "createdBy": _actor_label(revision.created_by),
        "definitionStatus": 'current_definition' if revision.metric_snapshot.get('contract', {}).get('sessionStructureVersion') == STRUCTURE_VERSION else 'historical_definition',
    }
    if include_content:
        result.update(
            {
                "title": revision.title,
                "summary": revision.summary,
                "facts": revision.facts,
                "mechanisms": revision.mechanisms,
                "associations": revision.associations,
                "hypotheses": revision.hypotheses,
                "actions": revision.actions,
                "metricSnapshot": revision.metric_snapshot,
                "sourceHashes": revision.source_hashes,
                "sourceStale": source_is_stale(revision),
            }
        )
    return result


def serialize_insight(insight: ManagementInsight, include_history: bool = False) -> dict[str, Any]:
    current = insight.revisions.select_related("created_by").get(revision=insight.current_revision)
    published = None
    if insight.published_revision is not None:
        published = insight.revisions.select_related("created_by").get(revision=insight.published_revision)
    result = {
        "id": str(insight.id),
        "productVersion": insight.product_version,
        "startDate": insight.start_date.isoformat(),
        "endDate": insight.end_date.isoformat(),
        "status": insight.status,
        "origin": insight.origin,
        "currentRevision": insight.current_revision,
        "publishedRevision": insight.published_revision,
        "createdBy": _actor_label(insight.created_by),
        "publishedBy": _actor_label(insight.published_by) if insight.published_by else None,
        "createdAt": insight.created_at.isoformat(),
        "updatedAt": insight.updated_at.isoformat(),
        "publishedAt": insight.published_at.isoformat() if insight.published_at else None,
        "current": serialize_revision(current),
        "published": serialize_revision(published) if published else None,
    }
    if include_history:
        result["history"] = [
            serialize_revision(item, include_content=False)
            for item in insight.revisions.select_related("created_by").all()
        ]
    return result

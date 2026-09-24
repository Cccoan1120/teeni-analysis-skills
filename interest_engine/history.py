from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from .contracts import product_identity
from .compatibility import interest_definition


def history_source_digest(snapshot: dict) -> str:
    def observed(value):
        if isinstance(value, dict):
            return {key: observed(item) for key, item in value.items()
                    if key not in {"historyBaseline", "aggregateCorrection", "sevenDayChange", "sevenDayGrowth", "sampleStatus"}}
        if isinstance(value, list):
            return [observed(item) for item in value]
        return value
    return hashlib.sha256(json.dumps(observed(snapshot), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def same_history_contract(left: dict, right: dict) -> bool:
    if product_identity(left) != product_identity(right):
        return False
    keys = ("registrySha256", "engineVersion", "detailSchema", "schemaVersion")
    def definition(item):
        method = item.get('method', {})
        return interest_definition({
            'engineVersion': item.get('engineVersion'), 'detailSchema': item.get('detailSchema'),
            **{key: method.get(key) for key in ('baseContract', 'baseCoreVersion', 'baseRulesVersion')},
        })
    return all(left.get(key) == right.get(key) for key in keys) and definition(left) == definition(right)


def history_maps(history: list[dict] | None, current: dict) -> tuple[dict[str, float], dict[str, dict]]:
    end = date.fromisoformat(current["dataDate"])
    start = end - timedelta(days=7)
    snapshots = {}
    for snapshot in history or []:
        observed = date.fromisoformat(snapshot["dataDate"])
        if start <= observed < end and same_history_contract(snapshot, current):
            snapshots[observed] = snapshot
    current["historyBaseline"] = {
        "startDate": start.isoformat(), "endDate": (end - timedelta(days=1)).isoformat(),
        "observedDays": len(snapshots),
        "snapshots": [{"dataDate": day.isoformat(), "sourceSha256": item.get("sourceSha256"),
                       "metricsSha256": history_source_digest(item)}
                      for day, item in sorted(snapshots.items())],
    }
    if not snapshots:
        return {}, {}
    entity_totals: dict[str, int] = {}
    candidate_values: dict[str, list[int]] = {}
    for snapshot in snapshots.values():
        for section in ("entities", "ipRollups", "restrictedEntities"):
            for row in snapshot.get(section, []):
                key = f"{section}:{row['id']}"
                entity_totals[key] = entity_totals.get(key, 0) + int(row.get("activeInterestUsers", 0))
        # Candidate lists are truncated: absence cannot establish a zero count.
        for row in snapshot.get("candidates", []):
            candidate_values.setdefault(row["normalizedPhrase"], []).append(int(row.get("users", 0)))
    return (
        {key: total / len(snapshots) for key, total in entity_totals.items()},
        {key: {"averageUsers": sum(values) / len(values)} for key, values in candidate_values.items()
         if len(values) == len(snapshots)},
    )


def repair_history_metrics(payload: dict, history: list[dict]) -> None:
    entities, candidates = history_maps(history, payload)
    for section in ("entities", "ipRollups", "restrictedEntities"):
        for row in payload.get(section, []):
            baseline = entities.get(f"{section}:{row['id']}")
            row["sevenDayChange"] = (row["activeInterestUsers"] - baseline) / baseline if baseline else None
    for row in payload.get("candidates", []):
        baseline = candidates.get(row["normalizedPhrase"], {}).get("averageUsers")
        row["sevenDayGrowth"] = row["users"] / baseline if baseline else None

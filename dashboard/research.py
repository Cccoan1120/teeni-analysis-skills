"""Local account behavior and independently reviewed conversation outcomes."""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import random
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from interest_engine.contracts import load_and_verify_input, sha256_file
from publisher.privacy import assert_aggregate_only

SCHEMA = "teeni-product-research/1.0.0"
DEMANDS = ("知识问答", "故事音乐", "游戏互动", "学习练习", "情绪与求助", "关系交流", "设备控制", "其他需求", "无法判定")
OUTCOMES = ("完整满足", "部分满足", "未满足", "无法判定")
SAMPLE_FIELDS = ("sample_id", "data_date", "sampling_group", "selection_probability", "conversation", "demand_type", "outcome", "reviewer", "reviewed_at", "notes")


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator, "rate": numerator / denominator if denominator else None}


def validate_report(payload):
    required = {'schemaVersion', 'productVersion', 'dataDate', 'sources', 'behavior', 'systemIntentDemand', 'evaluation', 'instrumentation'}
    if not required <= set(payload) or set(payload) - required - {'aiEvaluation', 'coverageConfirmation', 'evaluationProvenance'}:
        raise ValueError('unsupported research fields')
    if payload['schemaVersion'] != SCHEMA or payload['productVersion'] not in ('M1', 'M2'):
        raise ValueError('unsupported research contract')
    target = date.fromisoformat(payload['dataDate'])
    sources = payload['sources']
    dates = [row['dataDate'] for row in sources]
    if not dates or dates != sorted(set(dates)) or dates[-1] != target.isoformat():
        raise ValueError('invalid research source dates')
    for source in sources:
        date.fromisoformat(source['dataDate'])
        if set(source) != {'dataDate', 'detailSha256', 'manifestSha256', 'coverageComplete'} or type(source['coverageComplete']) is not bool:
            raise ValueError('invalid research source contract')
        if any(not re.fullmatch('[0-9a-f]{64}', source[key]) for key in ('detailSha256','manifestSha256')):
            raise ValueError('invalid research source hash')
    if 'evaluationProvenance' in payload:
        provenance = payload['evaluationProvenance']
        if set(provenance) != {'status', 'sources', 'sourceReportSha256', 'evaluationSha256'} or provenance['status'] != 'historical_definition':
            raise ValueError('invalid evaluation provenance contract')
        if not re.fullmatch('[0-9a-f]{64}', provenance['sourceReportSha256']):
            raise ValueError('invalid evaluation source report hash')
        old_sources = provenance['sources']
        old_dates = [source['dataDate'] for source in old_sources]
        if not old_dates or old_dates != sorted(set(old_dates)) or old_dates[-1] != payload['dataDate']:
            raise ValueError('invalid historical evaluation dates')
        for source in old_sources:
            date.fromisoformat(source['dataDate'])
            if set(source) != {'dataDate', 'detailSha256', 'manifestSha256', 'coverageComplete'} or type(source['coverageComplete']) is not bool:
                raise ValueError('invalid historical evaluation source')
            if any(not re.fullmatch('[0-9a-f]{64}', source[key]) for key in ('detailSha256', 'manifestSha256')):
                raise ValueError('invalid historical evaluation hash')
        if provenance['evaluationSha256'] != _evaluation_hash(payload):
            raise ValueError('historical evaluation content hash mismatch')
    def numeric(value):
        if isinstance(value, dict):
            if {'numerator', 'denominator'} <= value.keys():
                n, d = value['numerator'], value['denominator']
                if type(n) is not int or type(d) is not int or not 0 <= n <= d:
                    raise ValueError('invalid research ratio counts')
                expected = n / d if d else None
                actual = value['rate']
                if value.get('status') == 'observation_incomplete':
                    if actual is not None or value['observedRate'] != expected:
                        raise ValueError('incomplete observation must not become an authoritative rate')
                elif actual != expected:
                    raise ValueError('research ratio mismatch')
            for item in value.values(): numeric(item)
        elif isinstance(value, list):
            for item in value: numeric(item)
        elif type(value) in (int, float) and (not math.isfinite(value) or value < 0):
            raise ValueError('invalid research numeric value')
    numeric(payload)
    validate_evaluation(payload['evaluation'])
    if 'aiEvaluation' in payload:
        evaluated = payload['aiEvaluation']
        if evaluated.get('labelSource') != 'ai' or evaluated.get('humanReviewed') is not False:
            raise ValueError('AI evaluation requires explicit non-human provenance')
        if (evaluated['populationSessions'], evaluated['selectedSessions']) != (payload['evaluation']['populationSessions'], payload['evaluation']['selectedSessions']):
            raise ValueError('AI evaluation sample population mismatch')
        validate_evaluation(evaluated)
    if 'coverageConfirmation' in payload:
        confirmation = payload['coverageConfirmation']
        if set(confirmation) != {'basis', 'confirmedAt', 'confirmationSha256', 'dates'} or confirmation['basis'] != 'user_confirmation':
            raise ValueError('unsupported coverage confirmation')
        datetime.fromisoformat(confirmation['confirmedAt'])
        if not re.fullmatch('[0-9a-f]{64}', confirmation['confirmationSha256']):
            raise ValueError('invalid confirmation hash')
        if confirmation['dates'] != [source['dataDate'] for source in sources if source['coverageComplete']]:
            raise ValueError('confirmation dates mismatch')
    behavior = payload['behavior']
    if behavior['identity'] != 'account' or [row['date'] for row in behavior['daily']] != dates:
        raise ValueError('account observation dates mismatch')
    complete_dates = {source['dataDate'] for source in sources if source['coverageComplete']}
    for row in behavior['daily']:
        if row['firstObservedUsers'] + row['returningUsers'] != row['activeUsers']:
            raise ValueError('account observation counts mismatch')
    for cohort in behavior['cohorts']:
        for lag in (1, 7):
            start = date.fromisoformat(cohort['date'])
            window = {(start + timedelta(days=offset)).isoformat() for offset in range(lag + 1)}
            metric = cohort[f'd{lag}']
            if (metric['status'] == 'complete') != (window <= complete_dates):
                raise ValueError('cohort maturity contradicts source coverage')
    assert_aggregate_only(payload)


def validate_evaluation(evaluation):
    if not 0 <= evaluation['decidableSessions'] <= evaluation['reviewedSessions'] <= evaluation['selectedSessions'] <= evaluation['populationSessions']:
        raise ValueError('invalid research sample counts')
    rows = evaluation['rows']
    if len({row['label'] for row in rows}) != len(rows) or any(row['label'] not in DEMANDS for row in rows):
        raise ValueError('invalid demand labels')
    for row in rows:
        if row['complete'] + row['partial'] + row['unmet'] + row['unknown'] != row['reviewedSessions'] or row['reviewedUsers'] > row['reviewedSessions']:
            raise ValueError('demand outcome counts mismatch')
        if row['completion'] != ratio(row['complete'], row['reviewedSessions'] - row['unknown']):
            raise ValueError('demand completion mismatch')
    if sum(row['reviewedSessions'] for row in rows) != evaluation['reviewedSessions']:
        raise ValueError('reviewed sample reconciliation failed')
    if sum(row['reviewedSessions'] - row['unknown'] for row in rows) != evaluation['decidableSessions']:
        raise ValueError('decidable sample reconciliation failed')
    if evaluation['completion'] != ratio(sum(row['complete'] for row in rows), evaluation['decidableSessions']):
        raise ValueError('overall completion mismatch')
    if evaluation['coverage'] != ratio(evaluation['reviewedSessions'], evaluation['selectedSessions']):
        raise ValueError('review coverage mismatch')


def confirm_coverage(report, confirmation, confirmation_sha256):
    """Apply a source-bound user attestation without changing sealed samples."""
    if confirmation.get('basis') != 'user_confirmation' or confirmation.get('productVersion') != report['productVersion']:
        raise ValueError('coverage confirmation product or basis mismatch')
    datetime.fromisoformat(confirmation['confirmedAt'])
    expected = [{key: source[key] for key in ('dataDate', 'detailSha256', 'manifestSha256')} for source in report['sources']]
    if confirmation.get('sources') != expected:
        raise ValueError('coverage confirmation must bind the exact source packages')
    result = copy.deepcopy(report)
    complete_dates = {source['dataDate'] for source in result['sources']}
    for source in result['sources']:
        source['coverageComplete'] = True
    for cohort in result['behavior']['cohorts']:
        start = date.fromisoformat(cohort['date'])
        for lag in (1, 7):
            metric = cohort[f'd{lag}']
            window = {(start + timedelta(days=offset)).isoformat() for offset in range(lag + 1)}
            mature = window <= complete_dates
            metric.update(status='complete' if mature else 'observation_incomplete', rate=metric['observedRate'] if mature else None)
    last_day = date.fromisoformat(result['dataDate'])
    week = {(last_day - timedelta(days=offset)).isoformat() for offset in range(7)}
    result['behavior']['weekComplete'] = week <= complete_dates
    result['coverageConfirmation'] = {'basis': 'user_confirmation', 'confirmedAt': confirmation['confirmedAt'], 'confirmationSha256': confirmation_sha256, 'dates': sorted(complete_dates)}
    validate_report(result)
    return result


def _write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def longitudinal(days, complete_dates):
    """Maturity requires every daily export in a cohort's observation window."""
    first_seen = {}
    result = []
    for day in sorted(days):
        users = days[day]
        fresh = users - first_seen.keys()
        for user in fresh:
            first_seen[user] = day
        result.append({"date": day, "activeUsers": len(users), "firstObservedUsers": len(fresh), "returningUsers": len(users) - len(fresh)})
    cohorts = []
    for day in sorted(days):
        users = {user for user, first in first_seen.items() if first == day}
        row = {"date": day, "users": len(users)}
        for lag in (1, 7):
            start = date.fromisoformat(day)
            target = (start + timedelta(days=lag)).isoformat()
            window = {(start + timedelta(days=offset)).isoformat() for offset in range(lag + 1)}
            observed = len(users & days.get(target, set()))
            mature = window <= complete_dates
            row[f"d{lag}"] = {**ratio(observed, len(users)), "status": "complete" if mature else "observation_incomplete", "targetDate": target,
                                "rate": observed / len(users) if mature and users else None, "observedRate": observed / len(users) if users else None}
        cohorts.append(row)
    last_day = date.fromisoformat(max(days))
    week = {(last_day - timedelta(days=offset)).isoformat() for offset in range(7)}
    frequency = Counter(user for day, users in days.items() if day in week for user in users)
    return {"identity": "account", "firstSeenMethod": "first_observed_in_inputs", "daily": result, "cohorts": cohorts,
            "activeDayDistribution": [{"days": n, "users": sum(count == n for count in frequency.values())} for n in range(1, 8)],
            "weekCoveredDays": len(week & days.keys()), "weekComplete": week <= complete_dates}


def _read_inputs(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schemaVersion") != "teeni-research-input/1.0.0" or not isinstance(manifest.get("packages"), list) or not manifest["packages"]:
        raise ValueError("research manifest requires packages")
    entries, seen = [], set()
    for package in manifest["packages"]:
        day = date.fromisoformat(package["dataDate"]).isoformat()
        if day in seen:
            raise ValueError("duplicate data date")
        seen.add(day)
        detail = (manifest_path.parent / package["detailPath"]).resolve()
        base = (manifest_path.parent / package["manifestPath"]).resolve()
        if sha256_file(base) != package["manifestSha256"] or sha256_file(detail) != package["detailSha256"]:
            raise ValueError("input hash mismatch")
        contract = load_and_verify_input(detail, base)
        entries.append((day, package, contract))
    if len({entry[2].scene_id for entry in entries}) != 1:
        raise ValueError("mixed products are not allowed")
    entries.sort(key=lambda entry: entry[0])
    target_day = manifest.get("dataDate", entries[-1][0])
    if target_day != entries[-1][0]:
        raise ValueError("research target must be latest supplied date")
    days, complete_days = {}, set()
    session_rows = defaultdict(list)
    intent_users, intent_sessions, intent_turns = defaultdict(set), defaultdict(set), Counter()
    accounts, sources = set(), []
    for day, package, contract in entries:
        day_users, row_count, row_ids = set(), 0, set()
        with contract.detail_path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                row_count += 1
                if row.get("sceneId") != contract.scene_id or row.get("created_at", "")[:10] != day:
                    raise ValueError("detail scene/date mismatch")
                if not row.get("clientId") or not row.get("cid") or row["source_row"] in row_ids:
                    raise ValueError("invalid account/session/source row")
                row_ids.add(row["source_row"])
                eligible = row.get("user_query_reviewable") == "是"
                if eligible:
                    day_users.add(row["clientId"])
                if day != target_day:
                    continue
                key = (row["clientId"], row["cid"])
                session_rows[key].append(row)
                if eligible:
                    label = row.get("intention") or "未返回"
                    accounts.add(row["clientId"])
                    intent_users[label].add(row["clientId"])
                    intent_sessions[label].add(key)
                    intent_turns[label] += 1
        if row_count != contract.expected_rows:
            raise ValueError("detail row count mismatch")
        days[day] = day_users
        if package.get("coverageComplete") is True:
            complete_days.add(day)
        sources.append({"dataDate": day, "detailSha256": contract.detail_sha256, "manifestSha256": package["manifestSha256"],
                        "coverageComplete": package.get("coverageComplete") is True})
    demand = {"eligibleUsers": len(accounts), "method": "system_routing_labels",
              "rows": [{"label": label, "users": len(users), "sessions": len(intent_sessions[label]), "turns": intent_turns[label],
                        "penetration": ratio(len(users), len(accounts))} for label, users in sorted(intent_users.items(), key=lambda item: (-len(item[1]), item[0]))]}
    return entries[0][2].product_version, target_day, sources, days, complete_days, session_rows, demand


def _evaluation_hash(report):
    content = {key: report[key] for key in ('evaluation', 'aiEvaluation') if key in report}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def recompute_behavior_preserving_evaluation(manifest_path, legacy_report):
    """Recompute behavior without selecting, writing, or rebinding review samples."""
    legacy_path = Path(legacy_report)
    previous = json.loads(legacy_path.read_text(encoding='utf-8-sig'))
    validate_report(previous)
    product, target, sources, days, _, _, demand = _read_inputs(manifest_path)
    if (product, target) != (previous['productVersion'], previous['dataDate']):
        raise ValueError('historical evaluation product/date mismatch')
    result = copy.deepcopy(previous)
    for source in sources:
        source['coverageComplete'] = False
    result.update(sources=sources, behavior=longitudinal(days, set()), systemIntentDemand=demand)
    result.pop('coverageConfirmation', None)
    result['evaluationProvenance'] = copy.deepcopy(previous.get('evaluationProvenance')) or {
        'status': 'historical_definition', 'sources': copy.deepcopy(previous['sources']),
        'sourceReportSha256': sha256_file(legacy_path), 'evaluationSha256': _evaluation_hash(previous),
    }
    validate_report(result)
    return result


def prepare(manifest_path, output_dir, sample_size=120):
    output_dir = Path(output_dir).resolve()
    if not 1 <= sample_size <= 1000:
        raise ValueError("sample size must be 1..1000")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("output directory must be new or empty")
    product, target_day, sources, days, complete_days, session_rows, demand = _read_inputs(manifest_path)
    # Uniform session sampling within product/date; keep immutable selection evidence private.
    population = [(key, rows) for key, rows in session_rows.items() if any(row.get("user_query_reviewable") == "是" for row in rows)]
    population.sort(key=lambda item: item[0])
    seed = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    chosen = random.Random(seed).sample(population, min(sample_size, len(population)))
    sampling_probability = len(chosen) / len(population) if population else 0
    private_samples, sealed = [], {}
    for key, rows in chosen:
        rows.sort(key=lambda row: (int(row.get("turn_index") or 0), int(row["source_row"])))
        sid = hashlib.sha256((seed + json.dumps(key)).encode()).hexdigest()[:20]
        transcript = json.dumps([{"turn": row.get("turn_index"), "user": row.get("text", ""), "assistant": row.get("ai_text", "")} for row in rows], ensure_ascii=False)
        fixed = {"sample_id": sid, "data_date": target_day, "sampling_group": f"{product}/{target_day}",
                 "selection_probability": str(sampling_probability), "conversation": transcript}
        private_samples.append({**fixed, "demand_type": "", "outcome": "", "reviewer": "", "reviewed_at": "", "notes": ""})
        sealed[sid] = {**fixed, "account": hashlib.sha256((seed + key[0]).encode()).hexdigest()}
    report = {"schemaVersion": SCHEMA, "productVersion": product, "dataDate": target_day,
              "sources": sources, "behavior": longitudinal(days, complete_days),
              "systemIntentDemand": demand,
              "evaluation": {"status": "awaiting_review", "populationSessions": len(population), "selectedSessions": len(chosen),
                             "reviewedSessions": 0, "decidableSessions": 0, "coverage": ratio(0, len(chosen)), "completion": ratio(0, 0), "rows": []},
              "instrumentation": {"latency": "not_collected", "speechRecognition": "not_collected", "speechPlayback": "not_collected", "toolExecution": "not_collected"}}
    validate_report(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "research-summary.json", report)
    with (output_dir / "review-samples.private.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SAMPLE_FIELDS)
        writer.writeheader()
        writer.writerows(private_samples)
    _write_json(output_dir / "review-selection.private.json", {"schemaVersion": "teeni-review-selection/1.0.0", "summarySha256": sha256_file(output_dir / "research-summary.json"), "samples": sealed})
    return report


def review(directory, annotated_path, output_path, label_source='human'):
    if label_source not in ('human', 'ai'):
        raise ValueError('unsupported label source')
    directory = Path(directory)
    report_path = directory / "research-summary.json"
    seal = json.loads((directory / "review-selection.private.json").read_text(encoding="utf-8"))
    if sha256_file(report_path) != seal["summarySha256"]:
        raise ValueError("original research summary changed")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    seen, reviewed = set(), []
    with Path(annotated_path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            sid = row.get("sample_id")
            if sid in seen or sid not in seal["samples"]:
                raise ValueError("unknown or duplicate sample")
            seen.add(sid)
            for field in SAMPLE_FIELDS[:5]:
                if row.get(field) != seal["samples"][sid][field]:
                    raise ValueError("sample or selection probability changed")
            if not row.get("outcome"):
                continue
            if row.get('label_source', 'human') != label_source:
                raise ValueError('annotation provenance differs from requested label source')
            if row["outcome"] not in OUTCOMES or row.get("demand_type") not in DEMANDS or not row.get("reviewer", "").strip() or not row.get("reviewed_at", "").strip():
                raise ValueError("completed reviews require allowed labels, reviewer and review time")
            datetime.fromisoformat(row["reviewed_at"].strip())
            reviewed.append((row, seal["samples"][sid]["account"]))
    if seen != seal["samples"].keys():
        raise ValueError("annotated file must retain every sampled row")
    rows = []
    for demand in DEMANDS:
        group = [(row, account) for row, account in reviewed if row["demand_type"] == demand]
        if not group:
            continue
        counts = Counter(row["outcome"] for row, _ in group)
        decidable = len(group) - counts["无法判定"]
        rows.append({"label": demand, "reviewedSessions": len(group), "reviewedUsers": len({account for _, account in group}),
                     "complete": counts["完整满足"], "partial": counts["部分满足"], "unmet": counts["未满足"], "unknown": counts["无法判定"],
                     "completion": ratio(counts["完整满足"], decidable), "unmetUsers": len({account for row, account in group if row["outcome"] == "未满足"})})
    complete = sum(row["outcome"] == "完整满足" for row, _ in reviewed)
    decidable = sum(row["outcome"] != "无法判定" for row, _ in reviewed)
    evaluation = report["evaluation"] if label_source == 'human' else copy.deepcopy(report['evaluation'])
    evaluation.update(status="reviewed_sample" if reviewed else "awaiting_review", reviewedSessions=len(reviewed), decidableSessions=decidable,
                      coverage=ratio(len(reviewed), evaluation["selectedSessions"]), completion=ratio(complete, decidable), rows=rows,
                      interpretation="reviewed_sample_only", reviewedFileSha256=sha256_file(annotated_path))
    if label_source == 'ai':
        evaluation.update(labelSource='ai', humanReviewed=False, status='ai_reviewed_sample', interpretation='ai_sample_estimate_not_human_ground_truth')
        report['aiEvaluation'] = evaluation
    assert_aggregate_only(report)
    output_path = Path(output_path)
    if output_path.exists():
        raise ValueError("review output must be a new file")
    _write_json(output_path, report)
    return report


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("prepare")
    start.add_argument("--manifest", required=True)
    start.add_argument("--output-dir", required=True)
    start.add_argument("--sample-size", type=int, default=120)
    finish = commands.add_parser("review")
    finish.add_argument("--directory", required=True)
    finish.add_argument("--annotations", required=True)
    finish.add_argument("--output", required=True)
    finish.add_argument("--label-source", choices=('human', 'ai'), default='human')
    args = parser.parse_args()
    payload = prepare(args.manifest, args.output_dir, args.sample_size) if args.command == "prepare" else review(args.directory, args.annotations, args.output, args.label_source)
    print(json.dumps({"productVersion": payload["productVersion"], "dataDate": payload["dataDate"], "evaluation": payload["evaluation"]["status"]}))


if __name__ == "__main__":
    main()

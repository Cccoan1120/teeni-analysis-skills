"""Private, stratified human annotation preparation and aggregate semantic scoring."""

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from .classifier import Behavior, InterestSignal, QueryRoute
from .contracts import sha256_file

FIELDS = ("route", "behavior", "entity_names", "entity_signals", "preference_polarities")
SOURCE_FIELDS = ("sample_id", "source_row", "context_before", "user_query", "ai_reply")
LABEL_FIELDS = tuple("expected_" + field for field in FIELDS)
REVIEW_FIELDS = ("label_source", "annotator_1", "annotator_2", "adjudicator", "review_status", "notes")


def _signature(row):
    return hashlib.sha256(json.dumps([row.get(key, "") for key in SOURCE_FIELDS], ensure_ascii=False).encode()).hexdigest()


def prepare(detail, output, per_stratum=30):
    if per_stratum < 1:
        raise ValueError("per-stratum must be positive")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("semantic-review.private.csv", "semantic-predictions.private.json")):
        raise ValueError("review output already exists; preserve existing human annotations")
    candidates, context, populations = defaultdict(list), {}, Counter()
    source_hash = sha256_file(Path(detail))
    with Path(detail).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            strata = ["entity_hit" if row["entity_names"] else "entity_miss"]
            if row["behavior"] == Behavior.CORRECTION.value:
                strata.append("correction")
            if row["behavior"] == Behavior.UNKNOWN.value:
                strata.append("unknown_behavior")
            if "single_context" in row.get("match_bases", ""):
                strata.append("context_inherited")
            for polarity in ("positive", "negative", "mixed"):
                if polarity in row.get("preference_polarities", "").split("|"):
                    strata.append("preference_" + polarity)
            session = (row.get("clientId", ""), row.get("cid", "") or row["source_row"])
            sample_id = hashlib.sha256((source_hash + ":" + row["source_row"]).encode()).hexdigest()[:24]
            sample = {"sample_id": sample_id, "source_row": row["source_row"],
                      "context_before": context.get(session, ""), "user_query": row["text"], "ai_reply": row["ai_text"]}
            context[session] = json.dumps({"user": row["text"], "ai": row["ai_text"]}, ensure_ascii=False)
            prediction = {"signature": _signature(sample), "strata": strata,
                          **{field: row.get(field, "") for field in FIELDS}}
            for stratum in strata:
                populations[stratum] += 1
                candidates[stratum].append((sample_id, sample, prediction))
                candidates[stratum].sort(key=lambda item: item[0])
                del candidates[stratum][per_stratum:]
    selected = {item[0]: item for items in candidates.values() for item in items}
    with (output / "semantic-review.private.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=(*SOURCE_FIELDS, *LABEL_FIELDS, *REVIEW_FIELDS))
        writer.writeheader()
        for _, sample, _ in sorted(selected.values()):
            writer.writerow({**sample, "review_status": "pending"})
    predictions = {"schemaVersion": "teeni-interest-semantic-review/1.0.0", "sourceSha256": source_hash,
                   "populationByStratum": dict(populations), "samples": {key: item[2] for key, item in selected.items()}}
    (output / "semantic-predictions.private.json").write_text(json.dumps(predictions, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"preparedSamples": len(selected), "humanReviewedSamples": 0, "populationByStratum": dict(populations)}


def evaluate(labels, predictions, label_source='human'):
    if label_source not in ('human', 'ai'):
        raise ValueError('unsupported label source')
    package = json.loads(Path(predictions).read_text(encoding="utf-8"))
    if package.get("schemaVersion") != "teeni-interest-semantic-review/1.0.0":
        raise ValueError("unsupported semantic review schema")
    expected = package["samples"]
    seen, reviewed, correct, strata = set(), 0, Counter(), Counter()
    true_positive = false_positive = false_negative = 0
    allowed = {"route": {value.value for value in QueryRoute},
               "behavior": {value.value for value in Behavior} | {""},
               "entity_signals": {value.value for value in InterestSignal},
               "preference_polarities": {"positive", "negative", "neutral", "mixed"}}
    with Path(labels).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            sample_id = row["sample_id"]
            if sample_id in seen or sample_id not in expected or _signature(row) != expected[sample_id]["signature"]:
                raise ValueError("semantic review source or sample identity changed")
            seen.add(sample_id)
            if row.get("review_status") != ('ai_reviewed' if label_source == 'ai' else 'reviewed'):
                continue
            if row.get("label_source") != label_source or not row.get("annotator_1", "").strip():
                raise ValueError(f"reviewed labels require {label_source} provenance and named annotator")
            if label_source == 'human' and (not row.get("annotator_2", "").strip() or row["annotator_1"].strip() == row["annotator_2"].strip()):
                raise ValueError("reviewed labels require a second independent human annotator")
            gold = {field: row.get("expected_" + field, "") for field in FIELDS}
            for field in ("route", "behavior"):
                if gold[field] not in allowed[field]:
                    raise ValueError("invalid human label: " + field)
            entities = gold["entity_names"].split("|") if gold["entity_names"] else []
            if len(entities) != len(set(entities)):
                raise ValueError('duplicate entity in reviewed labels')
            for field in ("entity_signals", "preference_polarities"):
                values = gold[field].split("|") if gold[field] else []
                if len(values) != len(entities) or any(value not in allowed[field] for value in values):
                    raise ValueError("invalid aligned human label: " + field)
            predicted = expected[sample_id]
            reviewed += 1
            strata.update(predicted["strata"])
            for field in ('route', 'behavior'):
                correct[field] += gold[field] == predicted[field]
            gold_set, predicted_set = set(entities), set(filter(None, predicted["entity_names"].split("|")))
            correct['entity_names'] += gold_set == predicted_set
            # Compare aligned labels by entity identity, independently of serialization order.
            predicted_entities = predicted['entity_names'].split('|') if predicted['entity_names'] else []
            for field in ('entity_signals', 'preference_polarities'):
                gold_values = gold[field].split('|') if gold[field] else []
                predicted_values = predicted[field].split('|') if predicted[field] else []
                correct[field] += dict(zip(entities, gold_values, strict=True)) == dict(zip(predicted_entities, predicted_values, strict=True))
            true_positive += len(gold_set & predicted_set)
            false_positive += len(predicted_set - gold_set)
            false_negative += len(gold_set - predicted_set)
    coverage = {key: {"population": population,
                      "sampled": sum(key in item["strata"] for item in expected.values()),
                      "reviewed": strata[key]} for key, population in package["populationByStratum"].items()}
    complete = seen == set(expected) and reviewed == len(expected) and bool(reviewed)
    result = {"schemaVersion": "teeni-interest-semantic-evaluation/1.0.0", "sourceSha256": package["sourceSha256"],
            "labelSource": label_source if reviewed else "not_reviewed", "humanReviewedSamples": reviewed if label_source == 'human' else 0,
            "sampledRows": len(expected), "reviewComplete": complete,
            "hitAndMissReviewed": strata["entity_hit"] > 0 and strata["entity_miss"] > 0,
            "samplingMethod": "deterministic_stratified_unweighted", "strata": coverage,
            "sampleAccuracy": {field: correct[field] / reviewed if reviewed else None for field in FIELDS},
            "entityPrecision": true_positive / (true_positive + false_positive) if true_positive + false_positive else None,
            "entityRecall": true_positive / (true_positive + false_negative) if true_positive + false_negative else None}
    if label_source == 'ai':
        result.update(aiReviewedSamples=reviewed, interpretation='agreement_with_ai_review_not_human_accuracy',
                      aiAgreement=result['sampleAccuracy'], aiEntityPrecision=result['entityPrecision'], aiEntityRecall=result['entityRecall'])
        result.update(sampleAccuracy={field: None for field in FIELDS}, entityPrecision=None, entityRecall=None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--detail", required=True)
    preparation.add_argument("--output", required=True)
    preparation.add_argument("--per-stratum", type=int, default=30)
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--labels", required=True)
    evaluation.add_argument("--predictions", required=True)
    evaluation.add_argument("--output", required=True)
    evaluation.add_argument("--label-source", choices=('human', 'ai'), default='human')
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.detail, args.output, args.per_stratum)
    else:
        result = evaluate(args.labels, args.predictions, args.label_source)
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

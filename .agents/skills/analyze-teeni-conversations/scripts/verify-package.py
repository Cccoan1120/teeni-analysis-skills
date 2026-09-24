"""Independently reconcile raw exports and detail rows, and inspect OOXML error cells."""
from __future__ import annotations

import csv
import json
import sys
from itertools import zip_longest
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))
from teeni_analysis_core.csv_utils import detect_csv_encoding

REQUIRED = {"id", "clientId", "cid", "text", "response", "timestamp", "intention", "subIntention", "created_at", "sceneId", "model"}
IDENTITY = ("id", "clientId", "cid", "text", "timestamp", "created_at", "sceneId")


def source_rows(source):
    with Path(source).open(encoding=detect_csv_encoding(source), newline="") as handle:
        header = handle.readline()
        handle.seek(0)
        delimiter = "\t" if header.count("\t") > header.count(",") else ","
        reader = csv.reader(handle, delimiter=delimiter)
        columns = next(reader, [])
        if not REQUIRED.issubset(columns) or len(columns) != len(set(columns)):
            raise ValueError("source columns are missing or duplicated")
        for index, values in enumerate(reader, 2):
            if len(values) == len(columns):
                yield index, dict(zip(columns, values))
                continue
            if delimiter != "\t":
                raise ValueError(f"source row {index} has an invalid column count; repair the export before analysis")
            # Older TSV exports contain unquoted tabs inside text/response fields.
            text_index = columns.index("text")
            timestamps = [position for position in range(text_index + 1, len(values) - 1)
                          if values[position].strip().isdigit() and 10 <= len(values[position].strip()) <= 13]
            responses = [position for position in range(text_index + 1, timestamps[-1] if timestamps else text_index + 1)
                         if values[position].lstrip().startswith(("{", "["))]
            if not timestamps or not responses:
                raise ValueError(f"source row {index} cannot be reconstructed")
            timestamp_index, response_index = timestamps[-1], responses[0]
            trailing = values[timestamp_index + 1:-1]
            if len(trailing) > 2:
                raise ValueError(f"source row {index} has ambiguous trailing columns")
            raw = dict.fromkeys(columns, "")
            raw.update(zip(columns[:text_index], values[:text_index]))
            raw.update(text="\t".join(values[text_index:response_index]),
                       response="\t".join(values[response_index:timestamp_index]),
                       timestamp=values[timestamp_index], created_at=values[-1],
                       intention=trailing[0] if trailing else "",
                       subIntention=trailing[1] if len(trailing) > 1 else "")
            yield index, raw


def reconcile(source, detail, scene):
    count = 0
    with Path(detail).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_item, row in zip_longest(source_rows(source), reader):
            if raw_item is None or row is None:
                raise ValueError("source/detail row count mismatch")
            index, raw = raw_item
            if raw["sceneId"].strip() != scene:
                raise ValueError(f"source scene mismatch at row {index}")
            if row["source_row"] != str(index):
                raise ValueError(f"source/detail ordering mismatch at row {index}")
            for field in IDENTITY:
                if row[field] != raw[field].strip():
                    raise ValueError(f"source/detail {field} mismatch at row {index}")
            if "model" in row and row["model"] != raw["model"].strip():
                raise ValueError(f"source/detail model mismatch at row {index}")
            try:
                payload = json.loads(raw["response"])
            except (ValueError, TypeError):
                payload = None
            generated = payload.get("generated_text") if isinstance(payload, dict) else None
            ai_text = generated.strip() if isinstance(generated, str) else ""
            status = "解析失败" if not isinstance(payload, dict) else "正常" if ai_text else "无纯文本"
            if row["ai_text"] != ai_text or row["parse_status"] != status:
                raise ValueError(f"source/detail AI response mismatch at row {index}")
            count += 1
    if not count:
        raise ValueError("source contains no data rows")
    return count


def formula_errors(workbook):
    errors = []
    with ZipFile(workbook) as archive:
        for name in archive.namelist():
            if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                continue
            with archive.open(name) as handle:
                for _, cell in ElementTree.iterparse(handle, events=("end",)):
                    if cell.tag.rsplit("}", 1)[-1] == "c":
                        if cell.get("t") == "e":
                            errors.append({"sheet": name, "cell": cell.get("r")})
                        cell.clear()
    return errors


def main():
    request = json.load(sys.stdin)
    sources = {item["role"]: reconcile(item["source"], item["detail"], item["sceneId"]) for item in request.get("sources", [])}
    errors = formula_errors(request["workbook"]) if request.get("workbook") else []
    if request.get("supplemental"):
        supplemental = json.loads(Path(request["supplemental"]).read_text(encoding="utf-8"))
        if supplemental["schemaVersion"] != "teeni-base-supplemental-metrics/1.0.0":
            raise ValueError("supplemental schema mismatch")
        for source in request["sources"]:
            verify_supplemental(source["detail"], supplemental[source["role"]], request["rules"])
    if request.get("structure"):
        structure = json.loads(Path(request["structure"]).read_text(encoding="utf-8"))
        for source in request["sources"]:
            verify_structure(source["detail"], structure[source["role"]])
    print(json.dumps({"sources": sources, "formulaErrors": errors}))


def verify_structure(detail, actual):
    # Deliberately do not call the analysis implementation or use cached totals.
    counts, owners, ids = {}, {}, set()
    with Path(detail).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            cid, user, identity = row["cid"], row["clientId"], row["id"]
            if not cid or not user or not identity or identity in ids:
                raise ValueError("session structure missing or duplicate identity")
            if cid in owners and owners[cid] != user:
                raise ValueError("session structure cross-user cid")
            owners[cid] = user
            ids.add(identity)
            counts[cid] = counts.get(cid, 0) + 1
    total, sessions = len(ids), len(counts)
    singles = sum(value == 1 for value in counts.values())
    multi = sum(value >= 2 for value in counts.values())
    multi_turns = sum(value for value in counts.values() if value >= 2)
    five = sum(value >= 5 for value in counts.values())
    expected = {"schemaVersion": "teeni-session-structure/1.0.0",
                "totalTurns": total, "totalSessions": sessions, "singleTurnSessions": singles,
                "multiTurnSessions": multi, "multiTurnTurns": multi_turns, "fivePlusSessions": five,
                "averageTurns": total / sessions if sessions else None,
                "multiTurnRate": multi / sessions if sessions else None,
                "multiTurnAverageTurns": multi_turns / multi if multi else None,
                "fivePlusRate": five / sessions if sessions else None}
    if actual != expected:
        raise ValueError("session structure counts or derived metrics mismatch")


def verify_supplemental(detail, actual, rules):
    from collections import Counter, defaultdict
    with Path(detail).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    scoreable = [row for row in rows if row["scoreable"] == "是"]
    requests = [row for row in rows if row["user_query_reviewable"] == "是"]
    def check_metric(value, count, total):
        expected = {"numerator": count, "denominator": total, "rate": count / total if total else None}
        if value != expected:
            raise ValueError(f"supplemental metric mismatch: {value}, expected {expected}")
    quality = actual["quality"]
    check_metric(quality["responseMissing"], sum(not row["ai_text"] for row in requests), len(requests))
    check_metric(quality["responseParseFailure"], sum(row["parse_status"] == "解析失败" for row in requests), len(requests))
    check_metric(quality["uniqueQualityCandidate"], sum(int(row["quality_issue_count"]) > 0 for row in scoreable), len(scoreable))
    check_metric(quality["uniqueSafetyCandidate"], sum(int(row["safety_issue_count"]) > 0 for row in scoreable), len(scoreable))
    events = sum(int(row["quality_issue_count"]) for row in scoreable)
    if quality["qualityCandidateEvents"] != events or quality["qualityCandidateEventsPer100Responses"] != (100 * events / len(scoreable) if scoreable else None):
        raise ValueError("supplemental quality event density mismatch")
    sessions = defaultdict(list)
    for row in rows:
        sessions[row["cid"]].append(row)
    real_sessions = []
    opening = Counter()
    continued = Counter()
    for session in sessions.values():
        session.sort(key=lambda row: int(row["turn_index"]))
        real = [row for row in session if row["user_query_reviewable"] == "是"]
        real_sessions.append(real)
        for index, row in enumerate(session):
            if row["is_template"] == "是" or not row["text"]:
                continue
            for name, hit in (("fallback", "反复要求重说" in row["quality_signals"]), ("correction", row["user_correction"] == "是")):
                if hit:
                    continued[name + "total"] += 1
                    continued[name + "next"] += bool(real and int(real[-1]["turn_index"]) > int(row["turn_index"]))
        active = None
        segments = []
        previous_template = False
        for row in session:
            template = row["is_template"] == "是"
            opening["rawOpeningRows"] += template
            if template and not previous_template:
                active = []
                segments.append(active)
            elif active is not None and row["user_query_reviewable"] == "是":
                active.append(row)
            previous_template = template
        for segment in segments:
            opening["exposures"] += 1
            opening["realUserReplies"] += bool(segment)
            first_good = bool(segment and segment[0]["ai_text"] and segment[0]["is_invalid_turn"] == "否")
            opening["firstEffectiveResponses"] += first_good
            good_count = sum(bool(row["ai_text"]) and row["is_invalid_turn"] == "否" for row in segment)
            opening["effectiveThreeTurns"] += first_good and good_count >= 3
            opening["effectiveFiveTurns"] += first_good and good_count >= 5
    for key in ("rawOpeningRows", "exposures", "realUserReplies", "firstEffectiveResponses", "effectiveThreeTurns", "effectiveFiveTurns"):
        if actual["openingFunnel"][key] != opening[key]:
            raise ValueError(f"supplemental opening mismatch: {key}")
    if actual["openingFunnel"]["deduplicatedRetryRows"] != opening["rawOpeningRows"] - opening["exposures"]:
        raise ValueError("supplemental opening retry mismatch")
    for key in ("realUserReplies", "firstEffectiveResponses", "effectiveThreeTurns", "effectiveFiveTurns"):
        if actual["openingFunnel"][key + "Rate"] != (opening[key] / opening["exposures"] if opening["exposures"] else None):
            raise ValueError(f"supplemental opening rate mismatch: {key}")
    for name in ("fallback", "correction"):
        check_metric(actual["continuationProxies"][name], continued[name + "next"], continued[name + "total"])
    for index, item in enumerate(actual["earlyExperience"]):
        reached = [session for session in real_sessions if len(session) > index]
        expected = {"turn": index + 1, "reachedSessions": len(reached),
                    "continuedSessions": sum(len(session) > index + 1 for session in reached),
                    "missingResponseSessions": sum(not session[index]["ai_text"] for session in reached),
                    "frictionCandidateSessions": sum(int(session[index]["quality_issue_count"]) > 0 for session in reached),
                    "firstFrictionSessions": sum(int(session[index]["quality_issue_count"]) > 0 and not any(int(row["quality_issue_count"]) for row in session[:index]) for session in reached)}
        for key, value in expected.items():
            if item[key] != value:
                raise ValueError(f"supplemental early experience mismatch: {key}")
        if item["observedStoppedSessions"] != len(reached) - expected["continuedSessions"]:
            raise ValueError("supplemental observed stopped sessions mismatch")
        for key in ("continuedSessions", "missingResponseSessions", "frictionCandidateSessions"):
            if item[key + "Rate"] != (expected[key] / len(reached) if reached else None):
                raise ValueError("supplemental early experience rate mismatch")
    import re
    import unicodedata
    from bisect import bisect_right
    normalize = lambda value: re.sub(r"[\s，。！？、,.!?；;：:“”\"'‘’（）()《》【】\[\]~～\-_/]+", "", unicodedata.normalize("NFKC", value).lower())
    config = rules["ending_analysis"]["fallback_reopen"]
    queries = set(map(normalize, config["trigger_queries"]))
    replies = {normalize(item["text"]) for item in config["fixed_replies"]}
    starts = defaultdict(list)
    for session in sessions.values():
        starts[session[0]["clientId"]].append(float(session[0]["timestamp"]))
    for values in starts.values():
        values.sort()
    watermark = max(float(row["timestamp"]) for row in rows)
    trigger_count = mature_count = mature_success = observed_success = 0
    window = int(config["window_seconds"])
    for session in sessions.values():
        final = session[-1]
        if len(session) < 5 or normalize(final["text"]) not in queries or normalize(final["ai_text"]) not in replies:
            continue
        trigger_count += 1
        timestamp = float(final["timestamp"])
        mature = watermark - timestamp >= window
        mature_count += mature
        user_starts = starts[final["clientId"]]
        index = bisect_right(user_starts, timestamp)
        success = index < len(user_starts) and 0 < user_starts[index] - timestamp <= window
        observed_success += success
        mature_success += mature and success
    expected_reopen = {"windowSeconds": window, "triggerSessions": trigger_count,
                       "matureTriggerSessions": mature_count, "immatureTriggerSessions": trigger_count - mature_count,
                       "matureReopenedSessions": mature_success, "matureSessionReopenRate": mature_success / mature_count if mature_count else None,
                       "observedReopenedSessions": observed_success, "observedSessionReopenRate": observed_success / max(1, trigger_count)}
    if actual["fallbackReopen"] != expected_reopen:
        raise ValueError("supplemental mature reopen mismatch")
    if actual["observation"]["exportWatermarkConfirmed"] or actual["observation"]["crossDaySessionsComplete"]:
        raise ValueError("supplemental falsely claims complete observation")
    model_rows = [{"model": model, "rows": count} for model, count in sorted(Counter(row["model"] for row in rows).items())]
    if actual["productModels"] != model_rows:
        raise ValueError("supplemental model rows mismatch")
    import math
    times = [float(row["timestamp"]) for row in rows]
    input_quality = actual["inputQuality"]
    expected_input = {"missingKeys": {key: sum(not row[key] for row in rows) for key in ("id", "clientId", "cid")},
                      "duplicateRecordIds": len(rows) - len({row["id"] for row in rows}),
                      "sessionUserConflicts": sum(len({row["clientId"] for row in session}) > 1 for session in sessions.values()),
                      "invalidTimestamps": sum(not math.isfinite(value) or value <= 0 for value in times),
                      "timestampUnit": "seconds", "createdAtTimezone": "unspecified_source_local_time",
                      "nonEpochTimestampRows": sum(math.isfinite(value) and 0 < value < 1000000000 for value in times),
                      "missingModelRows": sum(not row["model"] for row in rows)}
    if input_quality != expected_input:
        raise ValueError("supplemental input quality mismatch")


if __name__ == "__main__":
    main()

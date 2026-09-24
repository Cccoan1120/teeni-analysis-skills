"""Aggregate behavioral proxies with explicit populations and observation limits."""
from collections import Counter


def metric(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def build_metrics(sessions, reopen, input_quality):
    rows = [row for session in sessions for row in session]
    requests = [row for row in rows if row["user_query_reviewable"] == "是"]
    scoreable = [row for row in rows if row["scoreable"] == "是"]
    quality_events = sum(row["quality_issue_count"] for row in scoreable)
    quality = {
        "reviewableResponses": len(scoreable),
        "responseMissing": metric(sum(not row["ai_text"] for row in requests), len(requests)),
        "responseParseFailure": metric(sum(row["parse_status"] == "解析失败" for row in requests), len(requests)),
        "uniqueQualityCandidate": metric(sum(row["quality_issue_count"] > 0 for row in scoreable), len(scoreable)),
        "qualityCandidateEvents": quality_events,
        "qualityCandidateEventsPer100Responses": 100 * quality_events / len(scoreable) if scoreable else None,
        "uniqueSafetyCandidate": metric(sum(row["safety_issue_count"] > 0 for row in scoreable), len(scoreable)),
        "safetyCandidateEvents": sum(row["safety_issue_count"] for row in scoreable),
        "allQualityCandidateEvents": sum(row["quality_issue_count"] for row in rows),
    }
    opening_counts = Counter()
    early = [{"turn": turn, "reachedSessions": 0, "continuedSessions": 0,
              "missingResponseSessions": 0, "frictionCandidateSessions": 0,
              "firstFrictionSessions": 0} for turn in range(1, 5)]
    continuation = Counter()
    for session in sessions:
        real = [row for row in session if row["user_query_reviewable"] == "是"]
        first_friction = next((i for i, row in enumerate(real) if row["quality_issue_count"]), None)
        for index, item in enumerate(early):
            if len(real) > index:
                item["reachedSessions"] += 1
                item["continuedSessions"] += len(real) > index + 1
                item["missingResponseSessions"] += not bool(real[index]["ai_text"])
                item["frictionCandidateSessions"] += real[index]["quality_issue_count"] > 0
                item["firstFrictionSessions"] += first_friction == index
        for index, row in enumerate(session):
            if row["is_template"] == "是" or not (row.get("user_text") or row.get("text")):
                continue
            fallback = "反复要求重说" in row["quality_signals"]
            correction = row["user_correction"] == "是"
            next_real = bool(real and int(real[-1]["turn_index"]) > int(row["turn_index"]))
            for name, matched in (("fallback", fallback), ("correction", correction)):
                if matched:
                    continuation[name + "Events"] += 1
                    continuation[name + "Continued"] += next_real
        # Consecutive automatic opening retries form one exposure. Each block
        # owns only the requests before the next block, preventing double credit.
        blocks = []
        for index, row in enumerate(session):
            if row["is_template"] == "是":
                if not index or session[index - 1]["is_template"] != "是":
                    blocks.append(index)
                opening_counts["rawOpeningRows"] += 1
        for block_index, start in enumerate(blocks):
            end = blocks[block_index + 1] if block_index + 1 < len(blocks) else len(session)
            replies = [row for row in session[start + 1:end] if row["user_query_reviewable"] == "是"]
            effective = [row for row in replies if row["ai_text"] and row["is_invalid_turn"] == "否"]
            opening_counts["exposures"] += 1
            opening_counts["realUserReplies"] += bool(replies)
            opening_counts["firstEffectiveResponses"] += bool(replies and replies[0]["ai_text"] and replies[0]["is_invalid_turn"] == "否")
            opening_counts["effectiveThreeTurns"] += bool(replies and replies[0] in effective and len(effective) >= 3)
            opening_counts["effectiveFiveTurns"] += bool(replies and replies[0] in effective and len(effective) >= 5)
    opening = {name: opening_counts[name] for name in ("rawOpeningRows", "exposures", "realUserReplies", "firstEffectiveResponses", "effectiveThreeTurns", "effectiveFiveTurns")}
    opening["deduplicatedRetryRows"] = opening["rawOpeningRows"] - opening["exposures"]
    for name in ("realUserReplies", "firstEffectiveResponses", "effectiveThreeTurns", "effectiveFiveTurns"):
        opening[name + "Rate"] = metric(opening[name], opening["exposures"])["rate"]
    for item in early:
        reached = item["reachedSessions"]
        item["observedStoppedSessions"] = reached - item["continuedSessions"]
        for count_name in ("continuedSessions", "missingResponseSessions", "frictionCandidateSessions"):
            item[count_name + "Rate"] = metric(item[count_name], reached)["rate"]
    return {
        "quality": quality,
        "openingFunnel": opening,
        "earlyExperience": early,
        "continuationProxies": {name: metric(continuation[name + "Continued"], continuation[name + "Events"]) for name in ("fallback", "correction")},
        "fallbackReopen": {
            "windowSeconds": reopen.get("windowSeconds", 300),
            "triggerSessions": reopen.get("triggerSessions", 0),
            "matureTriggerSessions": reopen.get("matureTriggerSessions", 0),
            "immatureTriggerSessions": reopen.get("immatureTriggerSessions", 0),
            "matureReopenedSessions": reopen.get("matureReopenedSessions", 0),
            "matureSessionReopenRate": reopen.get("matureSessionReopenRate"),
            "observedReopenedSessions": reopen.get("reopenedSessions", 0),
            "observedSessionReopenRate": reopen.get("sessionReopenRate"),
        },
        "observation": {"status": "observed_file_only", "watermarkSource": "maximum_record_timestamp_proxy",
                        "exportWatermarkConfirmed": False, "crossDaySessionsComplete": False,
                        "userInputIdentification": "rule_based_reviewable_query_proxy_without_event_origin",
                        "interpretation": "Continuation and early stopping are observed-file proxies, not satisfaction, recovery or churn. Empty generated_text is missing text, not confirmed execution failure."},
        "inputQuality": input_quality,
        "productModels": [{"model": model, "rows": count} for model, count in sorted(Counter(row.get("model", "") for row in rows).items())],
    }

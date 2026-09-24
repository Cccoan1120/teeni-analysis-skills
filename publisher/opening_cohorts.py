"""Detached, source-bound opening cohorts; build_addon accepts base-ready dictionaries."""
import argparse
import csv
import hashlib
import itertools
import json
import math
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

from .session_structure import COUNTS, RATIOS, SCHEMA as STRUCTURE_SCHEMA
from .session_structure import file_sha256, from_detail, validate_structure

SCHEMA = "teeni-opening-cohorts/1.4.0"
PRIOR_DIRECTION_SCHEMA = "teeni-opening-cohorts/1.3.0"
PREVIOUS_SCHEMA = "teeni-opening-cohorts/1.2.0"
EARLY_DIRECTION_SCHEMA = "teeni-opening-cohorts/1.1.0"
LEGACY_SCHEMA = "teeni-opening-cohorts/1.0.0"
GROUP_KEYS = ("startPrompt", "legacyDefault", "other")
DIRECTION_KEYS = (
    "knowledge", "operation", "preference", "unfinished_topic", "birthday",
    "anniversary", "holiday", "night", "fallback", "general_interest",
)
DIRECTION_LABELS = {
    "knowledge": "知识计划",
    "operation": "运营计划/运营任务+任务标题",
    "preference": "高频兴趣",
    "unfinished_topic": "未完成任务/未完话题",
    "birthday": "生日",
    "anniversary": "纪念日",
    "holiday": "节日",
    "night": "夜间陪伴",
    "fallback": "默认日常开场",
    "general_interest": "通用兴趣",
}
PUNCTUATION = r"[\s，。！？、,.!?；;：:“”\"'‘’（）()《》【】\[\]~～\-_/]+"
RULES = {
    "placeholder": "##{startPrompt}##",
    "normalization": "NFKC,strip,lower,remove-regex",
    "punctuationRegex": PUNCTUATION,
    "legacyExact": [
        "围绕这个内容你可以问我一个相关的问题吗直接展示一句话简洁问题内容",
        "围绕这个内容你可以和我讨论下里面的知识吗直接展示一句话简洁问题内容",
        "围绕这个内容你可以和我讨论有趣的观点吗直接展示一句话简洁问题内容",
    ],
    "legacyPrefix": "和我打招呼并称呼我的名字",
    "legacyPrefixMatching": "trimmed-raw-startswith",
    "systemHints": ["小朋友请靠近我按住按键对话哦", "请靠近我不要堵住麦克风再说一遍哦"],
    "firstRowOrder": ["timestamp:float-invalid-to-zero", "created_at:trim", "source_row:int"],
    "reply": "nonempty-after-first-excluding-placeholder-legacy-and-system-hints",
}
RULES_SHA256 = hashlib.sha256(json.dumps(RULES, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
DIRECTION_RULES = {
    "source": "response.extra.opening_prompt on the chronological startPrompt row",
    "schema": "fixed-ten-directions",
    "order": list(DIRECTION_KEYS),
    "labels": DIRECTION_LABELS,
    "prefixPattern": r"^\s*([^\r\n=]{1,120})==",
    "prefixAliases": {
        "knowledge": ["知识计划"],
        "operation": ["运营计划", "运营任务+任务标题", "运营计划/运营任务+任务标题"],
        "preference": ["高频兴趣"],
        "unfinished_topic": ["未完成任务", "未完话题", "未完成任务/未完话题"],
        "birthday": ["生日"],
        "anniversary": ["纪念日"],
        "holiday": ["节日"],
        "night": ["夜间陪伴"],
        "fallback": ["默认日常开场"],
        "general_interest": ["通用兴趣"],
    },
    "operationPrefix": "运营任务+",
    "classification": "NFKC and trim only the text before the first ==; never inspect text after ==",
    "withoutSeparator": "unlabelled",
    "unsupportedPrefix": "unsupported",
    "shareDenominator": "startPrompt sessions",
}
DIRECTION_RULES_SHA256 = hashlib.sha256(json.dumps(DIRECTION_RULES, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
# Direction rules used by the 1.3.0 package. Keep accepting these packages when
# reading historical snapshots after adding the tenth direction.
PRIOR_DIRECTION_RULES_SHA256 = "8aa4f7db9a654fee5c34aa8b2bc0c7f68c4bf026a741ba30a808c06f73411bf9"
_PUNCTUATION = re.compile(PUNCTUATION)
_DIRECTION = re.compile(DIRECTION_RULES["prefixPattern"])
_HASH = re.compile(r"^[0-9a-f]{64}$")
SOURCE_KEYS = ("detailSha256", "manifestSha256", "sourceSha256", "workbookSha256", "calculatorSha256")
_DIRECTION_ALIASES = {
    unicodedata.normalize("NFKC", alias).strip().lower().replace(" ", ""): key
    for key, aliases in DIRECTION_RULES["prefixAliases"].items()
    for alias in aliases
}


def normalize(value):
    return _PUNCTUATION.sub("", unicodedata.normalize("NFKC", str(value or "").strip()).lower())


def classify(value):
    if str(value or "").strip() == RULES["placeholder"]:
        return "startPrompt"
    text = normalize(value)
    if text in RULES["legacyExact"] or str(value or "").strip().startswith(RULES["legacyPrefix"]):
        return "legacyDefault"
    return "other"


def is_reply(value):
    return bool(str(value or "").strip()) and classify(value) == "other" and normalize(value) not in RULES["systemHints"]


def _sort_key(row):
    try:
        stamp = float(row.get("timestamp") or 0)
    except (TypeError, ValueError):
        stamp = 0
    # NaN has no total ordering, so an export containing it cannot establish a first row.
    if not math.isfinite(stamp):
        raise ValueError("non-finite timestamp cannot determine first request")
    return stamp, str(row.get("created_at") or "").strip(), int(row.get("source_row") or 0)


def _rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"cid", "text", "timestamp", "created_at", "source_row"} <= set(reader.fieldnames or ()):
            raise ValueError("detail lacks opening cohort fields")
        yield from reader


def _source_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        header = stream.readline()
        delimiter = "\t" if "\t" in header else ","
        stream.seek(0)
        reader = csv.DictReader(stream, delimiter=delimiter)
        if not {"cid", "text", "response"} <= set(reader.fieldnames or ()):
            raise ValueError("raw source lacks opening direction fields")
        for source_row, row in enumerate(reader, start=2):
            yield source_row, row


def _direction_prefix(value):
    normalized = unicodedata.normalize("NFKC", value).strip().lower().replace(" ", "")
    if normalized.startswith("运营计划/运营任务+") and len(normalized) > len("运营计划/运营任务+"):
        return "operation"
    if normalized.startswith("运营任务+") and len(normalized) > len("运营任务+"):
        return "operation"
    return _DIRECTION_ALIASES.get(normalized)


def _direction_from_response(value):
    try:
        response = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        raise ValueError("startPrompt response is not valid JSON")
    if not isinstance(response, dict):
        raise ValueError("startPrompt response must be an object")
    extra = response.get("extra")
    opening = extra.get("opening_prompt") if isinstance(extra, dict) else None
    if not isinstance(opening, str) or not opening.strip():
        return "unlabelled", None
    opening = unicodedata.normalize("NFKC", opening).strip()
    match = _DIRECTION.match(opening)
    if not match:
        return "unlabelled", None
    key = _direction_prefix(match.group(1))
    if key:
        return "classified", (key, DIRECTION_LABELS[key])
    return "unsupported", None


def _source_openings(path):
    result = {}
    for source_row, row in _source_rows(path):
        if str(row.get("text") or "").strip() == RULES["placeholder"]:
            result[source_row] = (str(row.get("cid") or ""), row.get("response"))
    return result


def _structure(lengths):
    lengths = list(lengths)
    result = dict(schemaVersion=STRUCTURE_SCHEMA, totalTurns=sum(lengths), totalSessions=len(lengths),
                  singleTurnSessions=sum(n == 1 for n in lengths), multiTurnSessions=sum(n >= 2 for n in lengths),
                  multiTurnTurns=sum(n for n in lengths if n >= 2), fivePlusSessions=sum(n >= 5 for n in lengths))
    for name, (num, den) in RATIOS.items():
        result[name] = result[num] / result[den] if result[den] else None
    return result


def _groups(sessions):
    total = len(sessions)
    groups = []
    for key in GROUP_KEYS:
        selected = [s for s in sessions.values() if s["group"] == key]
        counts = _structure(s["turns"] for s in selected)
        counts.pop("schemaVersion")
        replies = sum(s["replied"] for s in selected)
        groups.append(dict(key=key, **counts, replySessions=replies,
                           replyRate=replies / len(selected) if selected else None,
                           sessionShare=len(selected) / total if total else None))
    return groups


def _direction_groups(sessions):
    selected = [state for state in sessions.values() if state["group"] == "startPrompt"]
    total = len(selected)
    directions = []
    for key in DIRECTION_KEYS:
        label = DIRECTION_LABELS[key]
        members = [state for state in selected if state.get("direction") == (key, label)]
        counts = _structure(state["turns"] for state in members)
        counts.pop("schemaVersion")
        replies = sum(state["replied"] for state in members)
        directions.append(dict(key=key, label=label, **counts, replySessions=replies,
                               replyRate=replies / len(members) if members else None,
                               sessionShare=len(members) / total if total else None))
    classified = sum(state.get("directionStatus") == "classified" for state in selected)
    unlabelled = sum(state.get("directionStatus") == "unlabelled" for state in selected)
    unsupported = sum(state.get("directionStatus") == "unsupported" for state in selected)
    coverage = dict(totalStartPromptSessions=total, classifiedSessions=classified,
                    unlabelledSessions=unlabelled, unsupportedPrefixSessions=unsupported,
                    coverageRate=classified / total if total else None)
    return directions, coverage


def _calculate(path, source_path):
    sessions = {}
    for row in _rows(path):
        key, text = _sort_key(row), row["text"]
        state = sessions.setdefault(row["cid"], {"first": key, "text": text, "source_row": int(row["source_row"]), "turns": 0, "eligible": 0})
        state["turns"] += 1
        state["eligible"] += is_reply(text)
        if key < state["first"]:
            state["first"], state["text"], state["source_row"] = key, text, int(row["source_row"])
    openings = _source_openings(source_path)
    for cid, state in sessions.items():
        state["group"] = classify(state["text"])
        state["replied"] = state["eligible"] - is_reply(state["text"]) > 0
        if state["group"] == "startPrompt":
            raw = openings.get(state["source_row"])
            if raw is None or raw[0] != cid:
                raise ValueError("startPrompt source row is missing from raw source")
            state["directionStatus"], state["direction"] = _direction_from_response(raw[1])
    diagnostics = {"emptyFirstSessions": sum(not s["text"].strip() for s in sessions.values()),
                   "systemHintFirstSessions": sum(normalize(s["text"]) in RULES["systemHints"] for s in sessions.values())}
    directions, coverage = _direction_groups(sessions)
    return _groups(sessions), directions, coverage, diagnostics, set(sessions)


def _check_ready(ready):
    if not isinstance(ready, dict) or ready.get("status") != "local_verified":
        raise ValueError("base-ready must be locally verified")
    if ready.get("productVersion") not in ("M1", "M2") or str(ready.get("sceneId")) != {"M1": "488", "M2": "904"}[ready["productVersion"]]:
        raise ValueError("base-ready product/scene mismatch")
    date.fromisoformat(ready["dataDate"])
    for role in ("detail", "manifest", "source", "workbook"):
        expected = ready.get(role + "Sha256")
        if not isinstance(expected, str) or not _HASH.fullmatch(expected) or file_sha256(ready[role + "Path"]) != expected:
            raise ValueError(f"{role} source hash mismatch")
    manifest = json.loads(Path(ready["manifestPath"]).read_text(encoding="utf-8-sig"))
    if manifest.get("schemaVersion") != "teeni-base-bundle-manifest/1.0.0" or manifest.get("mode") != "primary" or manifest.get("dataDate") != ready["dataDate"] or str(manifest.get("sceneId")) != str(ready["sceneId"]):
        raise ValueError("base manifest identity mismatch")
    for collection, role, source in (("sources", "primary", "source"), ("outputs", "primary_detail", "detail"), ("outputs", "workbook", "workbook")):
        entries = [x for x in manifest.get(collection, []) if x.get("role") == role]
        if len(entries) != 1 or entries[0].get("sha256") != ready[source + "Sha256"] or entries[0].get("file") != Path(ready[source + "Path"]).name:
            raise ValueError(f"manifest {role} binding mismatch")
        if source != "workbook" and entries[0].get("rows") != ready.get("rowCount"):
            raise ValueError("manifest row count mismatch")
        if source == "source" and str(entries[0].get("sceneId")) != str(ready["sceneId"]):
            raise ValueError("manifest primary scene mismatch")
    structure = from_detail(ready["detailPath"], ready["sceneId"], ready["dataDate"], source_row_start=None)
    if structure["totalTurns"] != ready.get("rowCount"):
        raise ValueError("detail row count mismatch")
    return structure


def build_addon(ready, previous_ready=None):
    """Build from verified base-ready mappings, including optional prior-day mapping."""
    totals = _check_ready(ready)
    groups, directions, coverage, diagnostics, cids = _calculate(ready["detailPath"], ready["sourcePath"])
    previous_date = (date.fromisoformat(ready["dataDate"]) - timedelta(days=1)).isoformat()
    cross = dict(status="unavailable", previousDate=previous_date, overlapSessions=None,
                 previousDetailSha256=None, previousManifestSha256=None)
    if previous_ready is not None:
        if previous_ready.get("dataDate") != previous_date or previous_ready.get("productVersion") != ready["productVersion"]:
            raise ValueError("previous source must be same product and immediately preceding day")
        _check_ready(previous_ready)
        previous_cids = {row["cid"] for row in _rows(previous_ready["detailPath"])}
        cross.update(status="verified", overlapSessions=len(cids & previous_cids),
                     previousDetailSha256=previous_ready["detailSha256"], previousManifestSha256=previous_ready["manifestSha256"])
    source = {key: ready[key] for key in SOURCE_KEYS if key != "calculatorSha256"}
    source["calculatorSha256"] = file_sha256(__file__)
    result = dict(schemaVersion=SCHEMA, structureVersion=STRUCTURE_SCHEMA, rulesSha256=RULES_SHA256,
                  directionRulesSha256=DIRECTION_RULES_SHA256,
                  dataDate=ready["dataDate"], productVersion=ready["productVersion"], sceneId=str(ready["sceneId"]),
                  source=source, groups=groups, startPromptDirections=directions, directionCoverage=coverage,
                  totals=totals, diagnostics=diagnostics, crossDay=cross)
    validate_opening_cohorts(result, totals)
    return result


def _check_ratio(actual, num, den, name):
    expected = num / den if den else None
    if expected is None:
        if actual is not None:
            raise ValueError(f"{name} requires null for no sample")
    elif type(actual) not in (int, float) or not math.isfinite(actual) or abs(actual - expected) > 1e-12:
        raise ValueError(f"{name} does not match counts")


def validate_opening_cohorts(value, structure=None):
    if not isinstance(value, dict):
        raise ValueError("invalid opening cohort schema/rules")
    schema = value.get("schemaVersion")
    expected_keys = {"schemaVersion", "structureVersion", "rulesSha256", "dataDate", "productVersion", "sceneId", "source", "groups", "totals", "diagnostics", "crossDay"}
    direction_schemas = (EARLY_DIRECTION_SCHEMA, PREVIOUS_SCHEMA, PRIOR_DIRECTION_SCHEMA, SCHEMA)
    if schema in direction_schemas:
        expected_keys |= {"directionRulesSha256", "startPromptDirections"}
    if schema in (PRIOR_DIRECTION_SCHEMA, SCHEMA):
        expected_keys.add("directionCoverage")
    if set(value) != expected_keys or schema not in (LEGACY_SCHEMA, *direction_schemas) or value.get("structureVersion") != STRUCTURE_SCHEMA or value.get("rulesSha256") != RULES_SHA256:
        raise ValueError("invalid opening cohort schema/rules")
    if schema == SCHEMA and value.get("directionRulesSha256") != DIRECTION_RULES_SHA256:
        raise ValueError("invalid opening direction rules")
    if schema == PRIOR_DIRECTION_SCHEMA and value.get("directionRulesSha256") != PRIOR_DIRECTION_RULES_SHA256:
        raise ValueError("invalid opening direction rules")
    try:
        day = date.fromisoformat(value["dataDate"])
    except (TypeError, ValueError):
        raise ValueError("invalid opening data date") from None
    if value["productVersion"] not in ("M1", "M2") or value["sceneId"] != {"M1": "488", "M2": "904"}[value["productVersion"]]:
        raise ValueError("opening cohort product/scene mismatch")
    if not isinstance(value["source"], dict) or set(value["source"]) != set(SOURCE_KEYS) or any(not isinstance(v, str) or not _HASH.fullmatch(v) for v in value["source"].values()):
        raise ValueError("invalid opening source binding")
    validate_structure(value["totals"])
    if structure is not None and value["totals"] != structure:
        raise ValueError("opening totals disagree with session structure")
    groups = value["groups"]
    if not isinstance(groups, list) or len(groups) != 3 or any(not isinstance(g, dict) for g in groups) or [g.get("key") for g in groups] != list(GROUP_KEYS):
        raise ValueError("opening groups must contain three ordered categories")
    for group in groups:
        if set(group) != {"key", *COUNTS, *RATIOS, "replySessions", "replyRate", "sessionShare"}:
            raise ValueError("invalid opening group fields")
        validate_structure({"schemaVersion": STRUCTURE_SCHEMA, **{k: group[k] for k in (*COUNTS, *RATIOS)}})
        if type(group["replySessions"]) is not int or not 0 <= group["replySessions"] <= group["multiTurnSessions"]:
            raise ValueError("reply sessions exceed multi-turn sessions")
        _check_ratio(group["replyRate"], group["replySessions"], group["totalSessions"], "replyRate")
        _check_ratio(group["sessionShare"], group["totalSessions"], value["totals"]["totalSessions"], "sessionShare")
    if any(sum(g[key] for g in groups) != value["totals"][key] for key in COUNTS):
        raise ValueError("opening groups do not reconcile with daily totals")
    if schema in direction_schemas:
        directions = value["startPromptDirections"]
        if not isinstance(directions, list) or any(not isinstance(item, dict) for item in directions):
            raise ValueError("invalid startPrompt directions")
        if len({item.get("key") for item in directions}) != len(directions):
            raise ValueError("startPrompt direction keys must be unique")
        if schema in (PREVIOUS_SCHEMA, PRIOR_DIRECTION_SCHEMA, SCHEMA):
            expected_keys = DIRECTION_KEYS if schema == SCHEMA else DIRECTION_KEYS[:-1]
            if [(item.get("key"), item.get("label")) for item in directions] != [(key, DIRECTION_LABELS[key]) for key in expected_keys]:
                raise ValueError("startPrompt directions must contain the fixed ordered categories")
        for item in directions:
            if set(item) != {"key", "label", *COUNTS, *RATIOS, "replySessions", "replyRate", "sessionShare"}:
                raise ValueError("invalid startPrompt direction fields")
            expected_keys = DIRECTION_KEYS if schema == SCHEMA else DIRECTION_KEYS[:-1]
            key_pattern = r"(?:direction-[0-9a-f]{16}|unlabelled|other-format)" if schema == EARLY_DIRECTION_SCHEMA else r"(?:" + "|".join(expected_keys) + r")"
            if not isinstance(item["key"], str) or not re.fullmatch(key_pattern, item["key"]):
                raise ValueError("invalid startPrompt direction key")
            label = item["label"]
            if not isinstance(label, str) or not label.strip() or len(label) > 80 or re.search(r"[\x00-\x1f<>]", label):
                raise ValueError("invalid startPrompt direction label")
            validate_structure({"schemaVersion": STRUCTURE_SCHEMA, **{key: item[key] for key in (*COUNTS, *RATIOS)}})
            if type(item["replySessions"]) is not int or not 0 <= item["replySessions"] <= item["multiTurnSessions"]:
                raise ValueError("direction reply sessions exceed multi-turn sessions")
            _check_ratio(item["replyRate"], item["replySessions"], item["totalSessions"], "direction replyRate")
            _check_ratio(item["sessionShare"], item["totalSessions"], groups[0]["totalSessions"], "direction sessionShare")
        if schema in (PRIOR_DIRECTION_SCHEMA, SCHEMA):
            coverage = value["directionCoverage"]
            coverage_keys = {"totalStartPromptSessions", "classifiedSessions", "unlabelledSessions", "unsupportedPrefixSessions", "coverageRate"}
            if not isinstance(coverage, dict) or set(coverage) != coverage_keys:
                raise ValueError("invalid startPrompt direction coverage")
            counts = [coverage[key] for key in ("totalStartPromptSessions", "classifiedSessions", "unlabelledSessions", "unsupportedPrefixSessions")]
            if any(type(count) is not int or count < 0 for count in counts):
                raise ValueError("invalid startPrompt direction coverage counts")
            if coverage["totalStartPromptSessions"] != groups[0]["totalSessions"] or sum(counts[1:]) != counts[0]:
                raise ValueError("startPrompt direction coverage does not reconcile")
            _check_ratio(coverage["coverageRate"], coverage["classifiedSessions"], coverage["totalStartPromptSessions"], "direction coverageRate")
            if sum(item["totalSessions"] for item in directions) != coverage["classifiedSessions"]:
                raise ValueError("classified startPrompt directions do not reconcile")
            for key in (*COUNTS, "replySessions"):
                if sum(item[key] for item in directions) > groups[0][key]:
                    raise ValueError("classified direction counts exceed startPrompt total")
        else:
            for key in (*COUNTS, "replySessions"):
                if sum(item[key] for item in directions) != groups[0][key]:
                    raise ValueError("startPrompt directions do not reconcile with startPrompt total")
    diagnostics = value["diagnostics"]
    if not isinstance(diagnostics, dict) or set(diagnostics) != {"emptyFirstSessions", "systemHintFirstSessions"} or any(type(v) is not int or v < 0 for v in diagnostics.values()) or sum(diagnostics.values()) > groups[2]["totalSessions"]:
        raise ValueError("invalid opening diagnostics")
    cross = value["crossDay"]
    if not isinstance(cross, dict) or set(cross) != {"status", "previousDate", "overlapSessions", "previousDetailSha256", "previousManifestSha256"} or cross["previousDate"] != (day - timedelta(days=1)).isoformat():
        raise ValueError("invalid previous-day binding")
    if cross["status"] == "unavailable":
        if any(cross[key] is not None for key in ("overlapSessions", "previousDetailSha256", "previousManifestSha256")):
            raise ValueError("unavailable previous day requires null")
    elif cross["status"] == "verified":
        if type(cross["overlapSessions"]) is not int or not 0 <= cross["overlapSessions"] <= value["totals"]["totalSessions"] or any(not isinstance(cross[k], str) or not _HASH.fullmatch(cross[k]) for k in ("previousDetailSha256", "previousManifestSha256")):
            raise ValueError("invalid verified previous-day counts/hashes")
    else:
        raise ValueError("invalid previous-day status")


def verify_addon(value, ready, previous_ready=None):
    """Independently sort all rows and aggregate each cid, without primary state logic."""
    totals = _check_ready(ready)
    validate_opening_cohorts(value, totals)
    for field in ("dataDate", "productVersion", "sceneId"):
        if str(value[field]) != str(ready[field]):
            raise ValueError("verification identity mismatch")
    if any(value["source"][k] != ready[k] for k in SOURCE_KEYS if k != "calculatorSha256") or value["source"]["calculatorSha256"] != file_sha256(__file__):
        raise ValueError("verification source/code mismatch")
    records = []
    for row in _rows(ready["detailPath"]):
        try:
            stamp = float(row["timestamp"] or 0)
        except ValueError:
            stamp = 0
        if not math.isfinite(stamp):
            raise ValueError("non-finite timestamp")
        records.append((row["cid"], stamp, row["created_at"].strip(), int(row["source_row"]), row["text"]))
    records.sort(key=lambda r: r[:4])
    counts = {key: dict.fromkeys((*COUNTS, "replySessions"), 0) for key in GROUP_KEYS}
    source_openings = _source_openings(ready["sourcePath"])
    direction_counts = {
        (key, DIRECTION_LABELS[key]): dict.fromkeys((*COUNTS, "replySessions"), 0)
        for key in DIRECTION_KEYS
    }
    direction_status = dict(classified=0, unlabelled=0, unsupported=0)
    empty, hint = 0, 0
    seen_cids = set()
    for cid, record_iter in itertools.groupby(records, key=lambda r: r[0]):
        rows = list(record_iter)
        seen_cids.add(cid)
        texts = [re.sub(PUNCTUATION, "", unicodedata.normalize("NFKC", r[4].strip()).lower()) for r in rows]
        is_legacy = lambda text, raw: text in RULES["legacyExact"] or raw.strip().startswith(RULES["legacyPrefix"])
        first = rows[0][4].strip()
        category = "startPrompt" if first == RULES["placeholder"] else "legacyDefault" if is_legacy(texts[0], first) else "other"
        empty += first == ""
        hint += texts[0] in RULES["systemHints"]
        n = len(rows)
        count = counts[category]
        count["totalTurns"] += n
        count["totalSessions"] += 1
        count["singleTurnSessions"] += n == 1
        count["multiTurnSessions"] += n >= 2
        count["multiTurnTurns"] += n if n >= 2 else 0
        count["fivePlusSessions"] += n >= 5
        count["replySessions"] += any(row[4].strip() and row[4].strip() != RULES["placeholder"] and not is_legacy(text, row[4]) and text not in RULES["systemHints"] for row, text in zip(rows[1:], texts[1:]))
        if category == "startPrompt":
            raw = source_openings.get(rows[0][3])
            if raw is None or raw[0] != cid:
                raise ValueError("independent startPrompt source binding failed")
            try:
                response = json.loads(raw[1] or "{}")
            except (TypeError, json.JSONDecodeError):
                raise ValueError("independent startPrompt response is not valid JSON") from None
            extra = response.get("extra") if isinstance(response, dict) else None
            opening = extra.get("opening_prompt") if isinstance(extra, dict) else None
            match = (re.match(r"^\s*([^\r\n=]{1,120})==", unicodedata.normalize("NFKC", opening).strip())
                     if isinstance(opening, str) and opening.strip() else None)
            direction_key = None
            if match:
                prefix = unicodedata.normalize("NFKC", match.group(1)).strip().lower().replace(" ", "")
                if prefix.startswith("运营计划/运营任务+") and len(prefix) > len("运营计划/运营任务+"):
                    direction_key = "operation"
                elif prefix.startswith("运营任务+") and len(prefix) > len("运营任务+"):
                    direction_key = "operation"
                else:
                    direction_key = _DIRECTION_ALIASES.get(prefix)
            status = "classified" if direction_key else "unsupported" if match else "unlabelled"
            direction_status[status] += 1
            if direction_key:
                direction = direction_counts[(direction_key, DIRECTION_LABELS[direction_key])]
                direction["totalTurns"] += n
                direction["totalSessions"] += 1
                direction["singleTurnSessions"] += n == 1
                direction["multiTurnSessions"] += n >= 2
                direction["multiTurnTurns"] += n if n >= 2 else 0
                direction["fivePlusSessions"] += n >= 5
                direction["replySessions"] += any(row[4].strip() and row[4].strip() != RULES["placeholder"] and not is_legacy(text, row[4]) and text not in RULES["systemHints"] for row, text in zip(rows[1:], texts[1:]))
    for group in value["groups"]:
        if any(group[key] != expected for key, expected in counts[group["key"]].items()):
            raise ValueError("independent opening group verification failed")
    if value["diagnostics"] != dict(emptyFirstSessions=empty, systemHintFirstSessions=hint):
        raise ValueError("independent opening diagnostic verification failed")
    if value["schemaVersion"] in (PRIOR_DIRECTION_SCHEMA, SCHEMA):
        actual_directions = {(item["key"], item["label"]): item for item in value["startPromptDirections"]}
        if set(actual_directions) != set(direction_counts):
            raise ValueError("independent opening directions differ")
        for key, expected in direction_counts.items():
            if any(actual_directions[key][field] != amount for field, amount in expected.items()):
                raise ValueError("independent opening direction verification failed")
        expected_coverage = dict(
            totalStartPromptSessions=sum(direction_status.values()),
            classifiedSessions=direction_status["classified"],
            unlabelledSessions=direction_status["unlabelled"],
            unsupportedPrefixSessions=direction_status["unsupported"],
            coverageRate=direction_status["classified"] / sum(direction_status.values()) if sum(direction_status.values()) else None,
        )
        if value["directionCoverage"] != expected_coverage:
            raise ValueError("independent opening direction coverage verification failed")
    if previous_ready is None:
        if value["crossDay"]["status"] != "unavailable":
            raise ValueError("previous source required for verified cross-day result")
    else:
        _check_ready(previous_ready)
        if previous_ready["dataDate"] != value["crossDay"]["previousDate"] or previous_ready["productVersion"] != ready["productVersion"]:
            raise ValueError("independent previous source identity mismatch")
        prior = {r["cid"] for r in _rows(previous_ready["detailPath"])}
        expected = dict(status="verified", previousDate=previous_ready["dataDate"], overlapSessions=len(seen_cids.intersection(prior)),
                        previousDetailSha256=previous_ready["detailSha256"], previousManifestSha256=previous_ready["manifestSha256"])
        if value["crossDay"] != expected:
            raise ValueError("independent cross-day verification failed")
    return dict(schemaVersion="teeni-opening-verification/1.4.0", status="passed", dataDate=value["dataDate"],
                productVersion=value["productVersion"], checks=["source-hashes", "record-identity", "independent-sorted-cid-aggregation", "structure-reconciliation", "reply-subset", "startPrompt-direction-source-binding", "strict-prefix-only-direction-classification", "startPrompt-direction-coverage-reconciliation", "diagnostics", "previous-day-binding"],
                totalTurns=totals["totalTurns"], totalSessions=totals["totalSessions"], rulesSha256=RULES_SHA256,
                source=value["source"])


def write_package(ready, previous_ready, output_dir):
    """Verify and write new detached artifacts; return the aggregate value."""
    value = build_addon(ready, previous_ready)
    verification = verify_addon(value, ready, previous_ready)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    files = {"opening-cohorts.json": value, "opening-verification.json": verification}
    for name in (*files, "opening-manifest.json"):
        if (output / name).exists():
            raise ValueError("refusing to replace existing addon artifacts")
    for name, payload in files.items():
        (output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = dict(schemaVersion="teeni-opening-manifest/1.4.0", dataDate=value["dataDate"], productVersion=value["productVersion"],
                    source=value["source"], rulesSha256=RULES_SHA256, rules=RULES,
                    directionRulesSha256=DIRECTION_RULES_SHA256, directionRules=DIRECTION_RULES, crossDay=value["crossDay"],
                    outputs=[dict(file=name, sha256=file_sha256(output / name)) for name in files])
    (output / "opening-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--previous-ready")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    ready = json.loads(Path(args.ready).read_text(encoding="utf-8-sig"))
    previous = json.loads(Path(args.previous_ready).read_text(encoding="utf-8-sig")) if args.previous_ready else None
    value = write_package(ready, previous, args.output_dir)
    print(json.dumps({"status": "passed", "dataDate": value["dataDate"], "productVersion": value["productVersion"], "totalSessions": value["totals"]["totalSessions"]}))


if __name__ == "__main__":
    main()

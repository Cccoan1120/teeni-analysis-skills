from __future__ import annotations

import csv
import json
import math
import re
import unicodedata
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .csv_utils import detect_csv_encoding
from . import quality
from .rules import load_rules, scene_capability
from .supplemental import build_metrics
from .session_structure import build_session_structure
from .game_context import game_exclusions
PRIMARY_SHEETS = [
    "结论总览",
    "剔除单轮开场白后分析",
    "剔除无效轮次后分析",
    "五轮以上结束分析",
    "五轮以上结束分析（不剔除无效）",
    "五轮以上结束明细",
    "五轮以上结束明细（不剔除无效）",
    "有效对话子意图对比",
    "真实意图分析",
    "新版问题总览",
    "新版开场分析",
    "新版开场明细",
    "新版质量问题",
    "新版安全问题",
    "需关注用户汇总",
    "用户风险表达明细",
    "优化指标体系",
    "产品迭代建议",
    "新版会话汇总",
    "分析口径",
]
DEMOGRAPHIC_SHEET = "年龄性别概览"
LOCATION_SHEET = "所在地分析"
CONSTELLATION_SHEET = "星座分析"
COMPARISON_SHEETS = [PRIMARY_SHEETS[0], "版本基准对比", *PRIMARY_SHEETS[1:]]
REQUIRED_COLUMNS = {
    "id",
    "clientId",
    "cid",
    "text",
    "response",
    "timestamp",
    "intention",
    "subIntention",
    "created_at",
    "sceneId",
    "model",
}
DETAIL_FIELDS = [
    "model",
    "source_row",
    "id",
    "clientId",
    "cid",
    "sceneId",
    "timestamp",
    "created_at",
    "text",
    "ai_text",
    "parse_status",
    "source_intention",
    "source_subIntention",
    "intent_name_raw",
    "intent_parse_status",
    "intention",
    "subIntention",
    "turn_index",
    "session_turns",
    "meaningful_turn_index",
    "session_meaningful_turns",
    "engaged_session",
    "is_opening_reply",
    "scoreable",
    "user_correction",
    "exact_repeat",
    "valid_exact_repeat",
    "quality_signals",
    "quality_issue_count",
    "safety_signals",
    "safety_issue_count",
    "user_query_reviewable",
    "user_safety_signals",
    "user_safety_issue_count",
    "user_review_priority",
    "is_template",
    "is_invalid_turn",
    "invalid_reason",
    "net_valid_turn_index",
    "session_net_valid_turns",
    "profile_age",
    "age_band",
    "profile_gender",
    "birthday",
    "city",
    "city_normalized",
    "constellation",
    "age_status",
    "gender_status",
    "birthday_status",
    "city_status",
    "profile_status",
]
ENDING_DETAIL_FIELDS = [
    "clientId",
    "cid",
    "原始轮数",
    "非模板轮数",
    "净有效轮数",
    "会话开始时间",
    "会话结束时间",
    "末轮源行",
    "末轮时间戳",
    "真实主意图",
    "真实子意图",
    "意图解析状态",
    "结束信号主类",
    "置信度",
    "分类证据",
    "末轮query",
    "末轮AI response",
    "AI继续追问",
    "导出边界风险",
    "末轮质量信号",
    "末轮安全信号",
    "末轮AI安全信号",
    "末轮用户风险信号",
    "末轮用户复核优先级",
]
METRICS_FRAMEWORK = [
    ["硬门槛", "可评分状态", "解析成功且有AI纯文本", "可评分轮次/总轮次", "越高越好", "全量"],
    ["硬门槛", "AI安全候选率", "AI response命中安全高召回规则", "AI安全候选/可评分AI回复", "越低越好", "全量"],
    ["硬门槛", "用户风险表达率", "用户query命中独立高召回规则", "风险表达query/可审核用户query", "越低越好", "全量"],
    ["审核", "需关注用户率", "至少一个query命中用户风险规则", "候选用户/有可审核query用户", "仅作复核排期", "全量"],
    ["开场", "开口率", "固定开场后出现下一条用户输入", "已开口/开场曝光", "越高越好", "全量"],
    ["开场", "强延续率", "固定开场后形成实质承接", "强延续/开场曝光", "越高越好", "全量"],
    ["参与", "真实参与率", "会话至少包含一条非模板用户输入", "参与会话/全部会话", "越高越好", "全量"],
    ["深度", "平均非模板轮数", "排除模板开场后的平均用户轮数", "非模板轮数/参与会话", "适度提高", "剔除开场会话后"],
    ["深度", "平均净有效轮数", "逐轮剔除确认无效内容后的平均轮数", "净有效轮数/有效会话", "适度提高", "剔除无效轮次后"],
    ["深度", "中位有效轮数", "参与会话有效轮数中位数", "P50", "适度提高", "剔除后"],
    ["深度", "五轮及以上占比", "有效轮数至少五轮", "五轮会话/参与会话", "越高越好", "剔除后"],
    ["质量", "用户纠错率", "用户明确纠正AI理解", "纠错信号/有效轮次", "越低越好", "剔除后"],
    ["质量", "AI复读率", "相邻AI回复完全相同", "复读/可比较相邻轮", "越低越好", "剔除后"],
    ["意图", "意图解析覆盖率", "真实意图字段正常拆分", "正常解析/全部轮次", "越高越好", "488/901"],
]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _count_chars(value: Any) -> int:
    return len(re.sub(r"\s", "", _text(value)))


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * fraction)]


def _row_sort_key(row: dict[str, Any]) -> tuple[float, str, int]:
    try:
        timestamp = float(row.get("timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    return timestamp, _text(row.get("created_at")), int(row.get("source_row") or 0)


def _response_fields(raw: Any) -> tuple[dict[str, Any] | None, str, str]:
    try:
        payload = raw if isinstance(raw, dict) else json.loads(_text(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, "", "解析失败"
    if not isinstance(payload, dict):
        return None, "", "解析失败"
    generated = payload.get("generated_text")
    if not isinstance(generated, str) or not generated.strip():
        return payload, "", "无纯文本"
    return payload, generated.strip(), "正常"


def _nested(payload: dict[str, Any], path: list[str]) -> Any:
    value: Any = payload
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


AGE_BANDS = (("1-2岁", 1, 2), ("3-4岁", 3, 4), ("5-6岁", 5, 6), ("7-9岁", 7, 9), ("10-17岁", 10, 17))
VALID_GENDERS = {"男", "女"}
CONSTELLATIONS = (
    "摩羯座", "水瓶座", "双鱼座", "白羊座", "金牛座", "双子座",
    "巨蟹座", "狮子座", "处女座", "天秤座", "天蝎座", "射手座",
)


def _parse_age(value: Any) -> tuple[int | None, bool]:
    candidate = _text(value)
    if not candidate:
        return None, False
    if not re.fullmatch(r"[+-]?\d+", candidate):
        return None, True
    age = int(candidate)
    return (age, False) if 1 <= age <= 120 else (None, True)


def _age_band(age: int | None) -> str:
    if age is None:
        return ""
    for label, lower, upper in AGE_BANDS:
        if lower <= age <= upper:
            return label
    return "18+" if age >= 18 else "异常"


def _parse_birthday(value: Any) -> tuple[date | None, bool]:
    candidate = _text(value)
    if not candidate:
        return None, False
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return None, True
    try:
        return date.fromisoformat(candidate), False
    except ValueError:
        return None, True


def _constellation(birthday: date | None) -> str:
    if birthday is None:
        return ""
    month_day = birthday.month * 100 + birthday.day
    if month_day >= 1222 or month_day <= 119:
        return "摩羯座"
    boundaries = (
        (218, "水瓶座"), (320, "双鱼座"), (419, "白羊座"),
        (520, "金牛座"), (621, "双子座"), (722, "巨蟹座"),
        (822, "狮子座"), (922, "处女座"), (1023, "天秤座"),
        (1122, "天蝎座"), (1221, "射手座"),
    )
    return next(label for upper, label in boundaries if month_day <= upper)


def _parse_city(value: Any) -> tuple[str | None, bool]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, False
    if not isinstance(value, str):
        return None, True
    candidate = unicodedata.normalize("NFKC", value).strip()
    if not candidate:
        return None, False
    if len(candidate) > 64 or re.search(r"[\x00-\x1f{}\[\]<>]", candidate):
        return None, True
    if len(candidate) > 1 and candidate.endswith("市"):
        candidate = candidate[:-1]
    return candidate, False


def _raw_profile_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _resolve_profile(values: set[Any], invalid: bool) -> tuple[str, Any | None]:
    if len(values) > 1:
        return "冲突", None
    if len(values) == 1 and invalid:
        return "含非法值", None
    if len(values) == 1:
        return "正常", next(iter(values))
    return ("非法", None) if invalid else ("缺失", None)


def _resolve_demographics(rows: list[dict[str, Any]]) -> None:
    ages: dict[str, set[int]] = defaultdict(set)
    genders: dict[str, set[str]] = defaultdict(set)
    invalid_ages: set[str] = set()
    invalid_genders: set[str] = set()
    birthdays: dict[str, set[date]] = defaultdict(set)
    cities: dict[str, set[str]] = defaultdict(set)
    invalid_birthdays: set[str] = set()
    invalid_cities: set[str] = set()
    for row in rows:
        uid = _text(row.get("clientId"))
        raw_age = row.pop("_raw_age", None)
        age, invalid_age = _parse_age(raw_age)
        if age is not None:
            ages[uid].add(age)
        if invalid_age:
            invalid_ages.add(uid)
        raw_gender = _text(row.pop("_raw_gender", None))
        if raw_gender in VALID_GENDERS:
            genders[uid].add(raw_gender)
        elif raw_gender:
            invalid_genders.add(uid)
        raw_birthday = row.pop("_raw_birthday", None)
        row["birthday"] = _raw_profile_value(raw_birthday)
        birthday, invalid_birthday = _parse_birthday(raw_birthday)
        if birthday is not None:
            birthdays[uid].add(birthday)
        if invalid_birthday:
            invalid_birthdays.add(uid)
        raw_city = row.pop("_raw_city", None)
        row["city"] = _raw_profile_value(raw_city)
        city, invalid_city = _parse_city(raw_city)
        if city is not None:
            cities[uid].add(city)
        if invalid_city:
            invalid_cities.add(uid)
    profiles: dict[str, dict[str, Any]] = {}
    for uid in {_text(row.get("clientId")) for row in rows}:
        age_status, age = _resolve_profile(ages.get(uid, set()), uid in invalid_ages)
        gender_status, gender = _resolve_profile(
            genders.get(uid, set()), uid in invalid_genders
        )
        birthday_status, birthday = _resolve_profile(
            birthdays.get(uid, set()), uid in invalid_birthdays
        )
        city_status, city = _resolve_profile(
            cities.get(uid, set()), uid in invalid_cities
        )
        profiles[uid] = {
            "profile_age": age if age is not None else "",
            "age_band": _age_band(age),
            "profile_gender": gender or "",
            "age_status": age_status,
            "gender_status": gender_status,
            "city_normalized": city or "",
            "constellation": _constellation(birthday),
            "birthday_status": birthday_status,
            "city_status": city_status,
            "profile_status": (
                "正常"
                if age_status == gender_status == "正常"
                else f"年龄{age_status};性别{gender_status}"
            ),
        }
    for row in rows:
        row.update(profiles[_text(row.get("clientId"))])


def _intent_fields(
    scene_id: str,
    payload: dict[str, Any] | None,
    source_main: str,
    source_sub: str,
    rules: dict[str, Any],
) -> tuple[str, str, str, str]:
    source = scene_capability(rules, scene_id).get("strict_intent")
    if not source:
        status = "source_columns" if source_main or source_sub else "missing"
        return source_main or "未返回", source_sub or "未返回", "", status
    if payload is None:
        return "解析异常", "解析异常", "", "response_json_error"
    raw_intent = _text(_nested(payload, list(source["response_path"])))
    if not raw_intent:
        return "未返回", "未返回", "", "missing"
    separator = str(source.get("separator") or "｜")
    if raw_intent.count(separator) != 1:
        return "解析异常", "解析异常", raw_intent, "malformed"
    main, sub = (_text(part) for part in raw_intent.split(separator, 1))
    if not main or not sub:
        return "解析异常", "解析异常", raw_intent, "malformed"
    return main, sub, raw_intent, "parsed"


def _read_csv(
    path: str | Path,
    expected_scene: str,
    rules: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = Path(path)
    rows: list[dict[str, Any]] = []
    start = ""
    end = ""
    with source.open("r", encoding=detect_csv_encoding(source), newline="") as handle:
        header = handle.readline()
        handle.seek(0)
        delimiter = "\t" if header.count("\t") > header.count(",") else ","
        reader = csv.reader(handle, delimiter=delimiter)
        fieldnames = next(reader, None)
        missing = sorted(REQUIRED_COLUMNS - set(fieldnames or []))
        if missing:
            raise ValueError(f"required CSV columns missing: {', '.join(missing)}")
        for source_row, values in enumerate(reader, start=2):
            if len(values) == len(fieldnames):
                raw = dict(zip(fieldnames, values))
            elif delimiter == "\t":
                text_index = fieldnames.index("text")
                timestamp_candidates = [
                    index for index in range(text_index + 1, len(values) - 1)
                    if values[index].strip().isdigit() and 10 <= len(values[index].strip()) <= 13
                ]
                response_candidates = [
                    index for index in range(text_index + 1, timestamp_candidates[-1] if timestamp_candidates else text_index + 1)
                    if values[index].lstrip().startswith(("{", "["))
                ]
                if not timestamp_candidates or not response_candidates:
                    raise ValueError(f"row {source_row} cannot be reconstructed from tab-delimited export")
                timestamp_index = timestamp_candidates[-1]
                response_index = response_candidates[0]
                trailing = values[timestamp_index + 1:-1]
                if len(trailing) > 2:
                    raise ValueError(f"row {source_row} has ambiguous trailing columns")
                raw = {name: "" for name in fieldnames}
                raw.update(zip(fieldnames[:text_index], values[:text_index]))
                raw["text"] = "\t".join(values[text_index:response_index])
                raw["response"] = "\t".join(values[response_index:timestamp_index])
                raw["timestamp"] = values[timestamp_index]
                raw["intention"] = trailing[0] if trailing else ""
                raw["subIntention"] = trailing[1] if len(trailing) > 1 else ""
                raw["created_at"] = values[-1]
            else:
                raise ValueError(
                    f"row {source_row} has {len(values)} columns, expected {len(fieldnames)}"
                )
            scene_id = _text(raw.get("sceneId"))
            if scene_id != str(expected_scene):
                raise ValueError(
                    f"sceneId mismatch: expected {expected_scene}, found {scene_id or '<empty>'}"
                )
            payload, ai_text, parse_status = _response_fields(raw.get("response"))
            capability = scene_capability(rules, scene_id)
            demographics = capability.get("demographics") or {}
            source_main = _text(raw.get("intention"))
            source_sub = _text(raw.get("subIntention"))
            main, sub, intent_raw, intent_status = _intent_fields(
                scene_id, payload, source_main, source_sub, rules
            )
            created_at = _text(raw.get("created_at"))
            start = min(value for value in (start, created_at) if value) if start and created_at else start or created_at
            end = max(end, created_at)
            rows.append(
                {
                    "source_row": source_row,
                    "id": _text(raw.get("id")),
                    "clientId": _text(raw.get("clientId")),
                    "cid": _text(raw.get("cid")),
                    "text": _text(raw.get("text")),
                    "timestamp": _text(raw.get("timestamp")),
                    "created_at": created_at,
                    "sceneId": scene_id,
                    "model": _text(raw.get("model")),
                    "source_intention": source_main,
                    "source_subIntention": source_sub,
                    "intention": main,
                    "subIntention": sub,
                    "intent_name_raw": intent_raw,
                    "intent_parse_status": intent_status,
                    "ai_text": ai_text,
                    "parse_status": parse_status,
                    "_raw_age": _nested(payload, list(demographics.get("age_path", []))) if payload else None,
                    "_raw_gender": _nested(payload, list(demographics.get("gender_path", []))) if payload else None,
                    "_raw_birthday": _nested(payload, list(demographics.get("birthday_path", []))) if payload else None,
                    "_raw_city": _nested(payload, list(demographics.get("city_path", []))) if payload else None,
                }
            )
    if not rows:
        raise ValueError("CSV has no data rows")
    missing_keys = {key: sum(not row[key] for row in rows) for key in ("id", "clientId", "cid")}
    duplicates = len(rows) - len({row["id"] for row in rows})
    cid_users = defaultdict(set)
    timestamps = []
    for row in rows:
        cid_users[row["cid"]].add(row["clientId"])
        try:
            value = float(row["timestamp"])
        except (ValueError, TypeError):
            value = float("nan")
        timestamps.append(value)
    invalid_times = sum(not math.isfinite(value) or value <= 0 for value in timestamps)
    conflicts = sum(len(users) > 1 for users in cid_users.values())
    input_quality = {"missingKeys": missing_keys, "duplicateRecordIds": duplicates,
                     "sessionUserConflicts": conflicts, "invalidTimestamps": invalid_times,
                     "timestampUnit": "seconds", "createdAtTimezone": "unspecified_source_local_time",
                     "nonEpochTimestampRows": sum(math.isfinite(value) and 0 < value < 1000000000 for value in timestamps),
                     "missingModelRows": sum(not row["model"] for row in rows)}
    if any(missing_keys.values()) or duplicates or conflicts or invalid_times:
        raise ValueError(f"Input identity/time quality check failed: {json.dumps(input_quality)}")
    if any(value >= 100000000000 for value in timestamps):
        raise ValueError("timestamp must use seconds; millisecond or mixed-unit timestamps require an explicit repaired source")
    _resolve_demographics(rows)
    return rows, {
        "sourceName": source.name,
        "rows": len(rows),
        "sceneId": str(expected_scene),
        "start": start,
        "end": end,
        "bytes": source.stat().st_size,
        "inputQuality": input_quality,
    }


def _ordered_sessions(rows: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_text(row.get("cid"))].append(row)
    return [sorted(items, key=_row_sort_key) for items in grouped.values()]


def _is_template(row: dict[str, Any]) -> bool:
    user_text = _text(row.get("user_text") or row.get("text"))
    return quality.is_fixed_opening(user_text)


_INVALID_PUNCTUATION = re.compile(
    r"[\s，。！？、,.!?；;：:“”\"'‘’（）()《》【】\[\]~～\-_/]+"
)


def _normalize_invalid_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _text(value)).lower()
    return _INVALID_PUNCTUATION.sub("", normalized)


def _timestamp(row: dict[str, Any]) -> float:
    try:
        return float(row.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0


def _invalid_side_reason(value: Any, rules: dict[str, Any]) -> str:
    if _text(value) in rules["opening"].get("exact_texts", []):
        return "系统占位开场模板"
    normalized = _normalize_invalid_text(value)
    if not normalized:
        return ""
    invalid_rules = rules["invalid_turns"]
    prefixes = [
        _normalize_invalid_text(item)
        for item in invalid_rules.get("fixed_opening_prefixes", [])
    ]
    if re.search(str(invalid_rules["topic_opening_pattern"]), normalized):
        return "话题占位开场模板"
    if any(normalized.startswith(prefix) for prefix in prefixes if prefix):
        return "固定开场前缀"
    if normalized in set(invalid_rules.get("exact_normalized_texts", [])):
        return "确认无效内容"
    return ""


def _invalid_reason(row: dict[str, Any], rules: dict[str, Any]) -> str:
    reasons = []
    for side, value in (
        ("用户", row.get("user_text") or row.get("text")),
        ("AI", row.get("ai_text")),
    ):
        reason = _invalid_side_reason(value, rules)
        if reason:
            reasons.append(f"{side}:{reason}")
    return "；".join(reasons)


def _prepare_rows(
    rows: list[dict[str, Any]],
    rules: dict[str, Any],
    label: str,
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    sessions = _ordered_sessions(rows)
    summaries = []
    for session in sessions:
        meaningful = [row for row in session if not _is_template(row)]
        for row in session:
            row["invalid_reason"] = _invalid_reason(row, rules)
            row["is_invalid_turn"] = "是" if row["invalid_reason"] else "否"
            user_text = _text(row.get("user_text"))
            row["user_query_reviewable"] = (
                "是" if user_text and not _invalid_side_reason(user_text, rules) else "否"
            )
        valid_rows = [row for row in session if row["is_invalid_turn"] == "否"]
        meaningful_index = 0
        valid_index = 0
        awaiting_opening_reply = False
        previous_valid_ai = ""
        for index, row in enumerate(session):
            user_text = _text(row.get("user_text"))
            ai_text = _text(row.get("ai_text"))
            template = _is_template(row)
            if not template:
                meaningful_index += 1
            invalid = row["is_invalid_turn"] == "是"
            if not invalid:
                valid_index += 1
            if template:
                awaiting_opening_reply = True
            opening_reply = bool(awaiting_opening_reply and not invalid)
            if opening_reply:
                awaiting_opening_reply = False
            valid_exact_repeat = bool(
                not invalid and ai_text and previous_valid_ai and ai_text == previous_valid_ai
            )
            if not invalid:
                previous_valid_ai = ai_text
            row.update(
                {
                    "turn_index": index + 1,
                    "session_turns": len(session),
                    "meaningful_turn_index": meaningful_index if not template else 0,
                    "session_meaningful_turns": len(meaningful),
                    "net_valid_turn_index": valid_index if not invalid else 0,
                    "session_net_valid_turns": len(valid_rows),
                    "engaged_session": "是" if meaningful else "否",
                    "is_opening_reply": "是" if opening_reply else "否",
                    "scoreable": "是" if ai_text and not template else "否",
                    "user_correction": "是" if quality.CORRECTION.search(user_text) else "否",
                    "exact_repeat": "是"
                    if index > 0 and ai_text and ai_text == _text(session[index - 1].get("ai_text"))
                    else "否",
                    "valid_exact_repeat": "是" if valid_exact_repeat else "否",
                    "is_template": "是" if template else "否",
                    "version": label,
                }
            )
        intent_counts = Counter(_text(row.get("intention")) or "未返回" for row in session)
        summaries.append(
            {
                "clientId": _text(session[0].get("clientId")),
                "cid": _text(session[0].get("cid")),
                "turns": len(session),
                "meaningfulTurns": len(meaningful),
                "netValidTurns": len(valid_rows),
                "engaged": bool(meaningful),
                "start": _text(session[0].get("created_at")),
                "end": _text(session[-1].get("created_at")),
                "firstUser": _text(session[0].get("user_text")),
                "lastUser": _text((valid_rows or session)[-1].get("user_text")),
                "mainIntention": intent_counts.most_common(1)[0][0],
                "correctionSignals": sum(row["user_correction"] == "是" for row in session),
                "exactRepeats": sum(row["exact_repeat"] == "是" for row in session),
                "missingText": sum(not _text(row.get("ai_text")) for row in session),
            }
        )
    summaries.sort(key=lambda item: (-int(item["turns"]), str(item["start"])))
    return sessions, summaries


def _opening_aggregate(items: list[Any], field: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        grouped[_text(getattr(item, field)) or "未知"].append(item)
    result = []
    for group, values in grouped.items():
        result.append(
            {
                "group": group,
                "exposures": len(values),
                "opened": sum(item.opened for item in values),
                "strong": sum(item.follow_label == "强延续" for item in values),
                "weak": sum(item.follow_label == "弱延续" for item in values),
                "switched": sum(item.follow_label == "换题" for item in values),
                "uncertain": sum(item.follow_label in {"待人工复核", "无效或无法判断"} for item in values),
                "understanding": sum(item.understanding_signal for item in values),
                "thirdTurn": sum(item.reached_third_turn for item in values),
                "fifthTurn": sum(item.reached_fifth_turn for item in values),
            }
        )
    return sorted(result, key=lambda item: -int(item["exposures"]))


def _issue_summary(items: list[Any], scoreable_rows: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        grouped[item.category].append(item)
    rank = {"严重": 0, "高风险": 1, "高": 2, "中": 3, "待复核": 4}
    result = []
    for category, values in grouped.items():
        result.append(
            {
                "category": category,
                "severity": values[0].severity,
                "count": len(values),
                "sessions": len({item.cid for item in values}),
                "users": len({item.client_id for item in values}),
                "rate": len(values) / max(1, scoreable_rows),
                "example": values[0].evidence,
                "suggestion": quality.ISSUE_DEFINITIONS.get(category, ("", ""))[1],
            }
        )
    return sorted(result, key=lambda item: (rank.get(str(item["severity"]), 9), -int(item["count"])))


def _attention_users(
    items: list[Any],
    rules: dict[str, Any],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        grouped[item.client_id].append(item)
    priority = rules["user_safety"]["priority"]
    p0_categories = {str(value) for value in priority["p0_categories"]}
    severity_rank = {"严重": 0, "高风险": 1, "高": 2, "中": 3, "待复核": 4}
    result = []
    for client_id, values in grouped.items():
        query_keys = {
            (item.cid, item.record_id, item.turn_index)
            for item in values
        }
        sessions = {item.cid for item in values}
        categories = {item.category for item in values}
        if categories & p0_categories:
            review_priority = "P0"
            reason = "命中P0类别：" + "、".join(sorted(categories & p0_categories))
        elif (
            len(query_keys) >= int(priority["p1_min_hit_queries"])
            or len(sessions) >= int(priority["p1_min_hit_sessions"])
            or len(categories) >= int(priority["p1_min_categories"])
        ):
            review_priority = "P1"
            reasons = []
            if len(query_keys) >= int(priority["p1_min_hit_queries"]):
                reasons.append(f"累计{len(query_keys)}条query")
            if len(sessions) >= int(priority["p1_min_hit_sessions"]):
                reasons.append(f"跨{len(sessions)}个会话")
            if len(categories) >= int(priority["p1_min_categories"]):
                reasons.append(f"涉及{len(categories)}类风险")
            reason = "；".join(reasons)
        else:
            review_priority = "P2"
            reason = "单次或低频高召回候选"
        highest = min(
            (item.severity for item in values),
            key=lambda value: severity_rank.get(value, 9),
        )
        ordered = sorted(values, key=lambda item: item.source_time)
        result.append({
            "reviewPriority": review_priority,
            "clientId": client_id,
            "hitQueries": len(query_keys),
            "hitSessions": len(sessions),
            "categoryCount": len(categories),
            "categories": "|".join(sorted(categories)),
            "highestSeverity": highest,
            "firstHitTime": ordered[0].source_time,
            "lastHitTime": ordered[-1].source_time,
            "example": ordered[0].evidence,
            "priorityReason": reason,
        })
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}
    result.sort(key=lambda item: str(item["clientId"]))
    result.sort(key=lambda item: str(item["lastHitTime"]), reverse=True)
    return sorted(
        result,
        key=lambda item: (
            priority_rank[item["reviewPriority"]],
            -int(item["hitSessions"]),
            -int(item["hitQueries"]),
            -int(item["categoryCount"]),
        ),
    )


def _cohort_metrics(
    sessions: list[list[dict[str, Any]]],
    quality_issues: list[Any],
    safety_issues: list[Any],
    mode: str,
) -> dict[str, Any]:
    if mode not in {"all", "meaningful", "valid"}:
        raise ValueError(f"unsupported cohort mode: {mode}")
    selected = [
        session
        for session in sessions
        if mode == "all"
        or (mode == "meaningful" and any(not _is_template(row) for row in session))
        or (mode == "valid" and any(row.get("is_invalid_turn") == "否" for row in session))
    ]
    rows = [
        row
        for session in selected
        for row in session
        if mode != "valid" or row.get("is_invalid_turn") == "否"
    ]
    raw_lengths = [len(session) for session in selected]
    meaningful_lengths = [
        sum(
            row.get("is_invalid_turn") == "否" if mode == "valid" else not _is_template(row)
            for row in session
        )
        for session in selected
    ]
    scoreable = sum(row.get("scoreable") == "是" for row in rows)
    meaningful_rows = sum(meaningful_lengths)
    record_ids = {
        _text(row.get("id")) or f"row-{row.get('source_row')}"
        for row in rows
    }
    cohort_quality = [item for item in quality_issues if item.record_id in record_ids]
    scoreable_keys = {
        (_text(row.get("cid")), _text(row.get("id")) or f"row-{row.get('source_row')}", int(row.get("turn_index") or 0))
        for row in rows if row.get("scoreable") == "是"
    }
    cohort_safety = [
        item for item in safety_issues
        if (item.cid, item.record_id, item.turn_index) in scoreable_keys
    ]
    scoreable_quality = [item for item in cohort_quality if (item.cid, item.record_id, item.turn_index) in scoreable_keys]
    unique_quality = len({(item.cid, item.record_id, item.turn_index) for item in scoreable_quality})
    users = {_text(row.get("clientId")) for row in rows}
    return {
        "rows": len(rows),
        "sessions": len(selected),
        "users": len(users),
        "meaningfulRows": meaningful_rows,
        "invalidRows": sum(row.get("is_invalid_turn") == "是" for session in selected for row in session),
        "netValidRows": sum(row.get("is_invalid_turn") == "否" for session in selected for row in session),
        "avgRawTurns": sum(raw_lengths) / max(1, len(raw_lengths)),
        "avgMeaningfulTurns": sum(meaningful_lengths) / max(1, len(meaningful_lengths)),
        "medianRawTurns": _percentile(raw_lengths, 0.5),
        "p90RawTurns": _percentile(raw_lengths, 0.9),
        "medianMeaningfulTurns": _percentile(meaningful_lengths, 0.5),
        "p90MeaningfulTurns": _percentile(meaningful_lengths, 0.9),
        "rawOneTurnShare": sum(value == 1 for value in raw_lengths) / max(1, len(raw_lengths)),
        "oneMeaningfulShare": sum(value == 1 for value in meaningful_lengths) / max(1, len(meaningful_lengths)),
        "twoFourShare": sum(2 <= value <= 4 for value in meaningful_lengths) / max(1, len(meaningful_lengths)),
        "fiveNineShare": sum(5 <= value <= 9 for value in meaningful_lengths) / max(1, len(meaningful_lengths)),
        "tenPlusShare": sum(value >= 10 for value in meaningful_lengths) / max(1, len(meaningful_lengths)),
        "fivePlusShare": sum(value >= 5 for value in meaningful_lengths) / max(1, len(meaningful_lengths)),
        "scoreableRows": scoreable,
        "scoreableShare": scoreable / max(1, len(rows)),
        "correctionRate": sum(row.get("user_correction") == "是" for row in rows) / max(1, meaningful_rows),
        "exactRepeatRate": sum(
            row.get("valid_exact_repeat" if mode == "valid" else "exact_repeat") == "是"
            for row in rows
        ) / max(1, len(rows) - len(selected)),
        "qualityCandidates": len(cohort_quality),
        "qualitySessions": len({item.cid for item in cohort_quality}),
        "qualityRate": unique_quality / scoreable if scoreable else None,
        "uniqueQualityCandidateResponses": unique_quality,
        "qualityCandidateEventsPer100Responses": 100 * len(scoreable_quality) / scoreable if scoreable else None,
        "safetyCandidates": len(cohort_safety),
        "safetySessions": len({item.cid for item in cohort_safety}),
        "safetyRate": len(cohort_safety) / max(1, scoreable),
        "bands": [
            {"label": "0轮", "count": sum(value == 0 for value in meaningful_lengths)},
            {"label": "1轮", "count": sum(value == 1 for value in meaningful_lengths)},
            {"label": "2-4轮", "count": sum(2 <= value <= 4 for value in meaningful_lengths)},
            {"label": "5-9轮", "count": sum(5 <= value <= 9 for value in meaningful_lengths)},
            {"label": "10轮以上", "count": sum(value >= 10 for value in meaningful_lengths)},
        ],
        "rawBands": [
            {"label": "1轮", "count": sum(value == 1 for value in raw_lengths)},
            {"label": "2-4轮", "count": sum(2 <= value <= 4 for value in raw_lengths)},
            {"label": "5-9轮", "count": sum(5 <= value <= 9 for value in raw_lengths)},
            {"label": "10轮以上", "count": sum(value >= 10 for value in raw_lengths)},
        ],
    }


def _ranked(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counts = Counter(_text(row.get(field)) or "未返回" for row in rows)
    total = len(rows)
    return [
        {"label": label, "count": count, "rate": count / max(1, total)}
        for label, count in counts.most_common()
    ]


def _subintent_comparison(
    valid_rows: list[dict[str, Any]], ending_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    all_status = Counter(_text(row.get("intent_parse_status")) or "missing" for row in valid_rows)
    ending_status = Counter(_text(row.get("意图解析状态")) or "missing" for row in ending_rows)
    all_parsed = [row for row in valid_rows if row.get("intent_parse_status") == "parsed"]
    ending_parsed = [row for row in ending_rows if row.get("意图解析状态") == "parsed"]
    all_counts = Counter(_text(row.get("subIntention")) for row in all_parsed)
    ending_counts = Counter(_text(row.get("真实子意图")) for row in ending_parsed)
    labels = set(all_counts) | set(ending_counts)
    rows = []
    for label in labels:
        all_count = all_counts[label]
        ending_count = ending_counts[label]
        all_rate = all_count / max(1, len(all_parsed))
        ending_rate = ending_count / max(1, len(ending_parsed))
        rows.append(
            {
                "label": label,
                "allCount": all_count,
                "allRate": all_rate,
                "endingCount": ending_count,
                "endingRate": ending_rate,
                "ppDifference": ending_rate - all_rate,
            }
        )
    rows.sort(key=lambda item: (-int(item["allCount"]), str(item["label"])))
    top_differences = sorted(
        rows,
        key=lambda item: (-abs(float(item["ppDifference"])), -int(item["endingCount"]), str(item["label"])),
    )[:10]
    return {
        "allValidTurns": len(valid_rows),
        "allParsedTurns": len(all_parsed),
        "endingSessions": len(ending_rows),
        "endingParsedSessions": len(ending_parsed),
        "allStatus": [
            {"status": status, "count": count}
            for status, count in all_status.most_common()
        ],
        "endingStatus": [
            {"status": status, "count": count}
            for status, count in ending_status.most_common()
        ],
        "rows": rows,
        "topDifferences": top_differences,
    }


def _write_detail(path: str | Path, rows: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DETAIL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda item: int(item.get("source_row") or 0)):
            writer.writerow({field: row.get(field, "") for field in DETAIL_FIELDS})


def _pattern_hits(pattern: re.Pattern[str], text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in pattern.finditer(text)))


def _ending_analysis(
    sessions: list[list[dict[str, Any]]],
    export_timestamp: float,
    rules: dict[str, Any],
    *,
    mode: str = "valid",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if mode not in {"valid", "raw"}:
        raise ValueError(f"unsupported ending analysis mode: {mode}")
    ending_rules = rules["ending_analysis"]
    minimum_turns = int(ending_rules["minimum_effective_turns"])
    boundary_seconds = int(ending_rules["boundary_window_seconds"])
    category_order = [str(value) for value in ending_rules["categories"]]
    patterns = {
        key: re.compile(str(ending_rules[key]))
        for key in (
            "closure_pattern",
            "acknowledgement_pattern",
            "device_or_switch_pattern",
            "question_pattern",
            "ai_followup_pattern",
        )
    }
    punctuation = re.compile(str(ending_rules["normalization_pattern"]))
    details: list[dict[str, Any]] = []
    for session in sessions:
        meaningful = [row for row in session if not _is_template(row)]
        valid_rows = [row for row in session if row.get("is_invalid_turn") == "否"]
        analysis_rows = valid_rows if mode == "valid" else session
        if len(analysis_rows) < minimum_turns:
            continue
        final = analysis_rows[-1]
        query = _text(final.get("user_text") or final.get("text"))
        ai_text = _text(final.get("ai_text"))
        normalized = punctuation.sub("", query)
        correction_hits = _pattern_hits(quality.CORRECTION, query)
        negative_hits = _pattern_hits(quality.NEGATIVE_FEEDBACK, query)
        difficulty_hits = _pattern_hits(quality.DIFFICULTY_SIGNAL, query)
        device_hits = _pattern_hits(patterns["device_or_switch_pattern"], query)
        closure_hits = _pattern_hits(patterns["closure_pattern"], query)
        acknowledgement_hits = _pattern_hits(patterns["acknowledgement_pattern"], normalized)
        question_hits = _pattern_hits(patterns["question_pattern"], query)
        ai_followup = bool(patterns["ai_followup_pattern"].search(ai_text))
        ai_failed = not ai_text or _text(final.get("parse_status")) != "正常"
        substantive = bool(
            len(normalized) >= 8
            or question_hits
            or (len(normalized) >= 4 and ai_followup)
        )

        evidence: list[str] = []
        if ai_failed:
            evidence.append(
                "AI状态: "
                f"parse_status={_text(final.get('parse_status')) or '未知'}, "
                f"scoreable={_text(final.get('scoreable')) or '否'}, "
                f"ai_text={'空' if not ai_text else '非空'}"
            )
        if correction_hits:
            evidence.append(f"纠错命中:{'|'.join(correction_hits)}")
        if negative_hits:
            evidence.append(f"负反馈命中:{'|'.join(negative_hits)}")
        if difficulty_hits:
            evidence.append(f"理解困难命中:{'|'.join(difficulty_hits)}")
        if device_hits:
            evidence.append(f"指令/换题命中:{'|'.join(device_hits)}")
        if closure_hits:
            evidence.append(f"结束表达命中:{'|'.join(closure_hits)}")
        if acknowledgement_hits:
            evidence.append(f"短确认命中:{'|'.join(acknowledgement_hits)}")
        if question_hits:
            evidence.append(f"用户问句命中:{'|'.join(question_hits)}")

        if ai_failed:
            category, confidence = category_order[0], "高"
        elif correction_hits or negative_hits or difficulty_hits:
            category = category_order[1]
            confidence = "高" if correction_hits or negative_hits else "中"
        elif device_hits:
            category, confidence = category_order[2], "高"
        elif closure_hits:
            category, confidence = category_order[3], "高"
        elif acknowledgement_hits:
            category, confidence = category_order[4], "中"
        elif substantive:
            category = category_order[5]
            confidence = "中" if question_hits else "低"
        else:
            category, confidence = category_order[6], "低"

        if category == category_order[5] and not question_hits:
            evidence.append(f"实质输入:去标点长度={len(normalized)}")
        elif category == category_order[6]:
            evidence.append(f"信息量不足:去标点长度={len(normalized)}")
        elif len(normalized) >= 4 and not acknowledgement_hits:
            evidence.append(f"实质输入:去标点长度={len(normalized)}")

        try:
            final_timestamp = float(final.get("timestamp") or 0)
        except (TypeError, ValueError):
            final_timestamp = 0
        boundary_risk = bool(
            final_timestamp
            and export_timestamp
            and 0 <= export_timestamp - final_timestamp <= boundary_seconds
        )
        quality_signals = []
        if _text(final.get("parse_status")) != "正常":
            quality_signals.append("AI响应解析失败")
        if not ai_text:
            quality_signals.append("AI空回复")
        if correction_hits:
            quality_signals.append("用户纠正")
        if quality.TTS_UNFRIENDLY.search(ai_text):
            quality_signals.append("AI TTS风险")
        if _count_chars(ai_text) > quality.LONG_REPLY_CHARS:
            quality_signals.append("AI回复过长")
        if negative_hits:
            quality_signals.append("用户负反馈")
        if quality.FALLBACK.search(ai_text):
            quality_signals.append("AI兜底回复")
        if final.get("exact_repeat") == "是":
            quality_signals.append("AI完全复读")
        ai_safety_signals = [
            value for value in _text(final.get("safety_signals")).split("|") if value
        ]
        user_safety_signals = [
            value for value in _text(final.get("user_safety_signals")).split("|") if value
        ]
        safety_signals = list(dict.fromkeys([*ai_safety_signals, *user_safety_signals]))
        detail = {
                "clientId": _text(final.get("clientId")),
                "cid": _text(final.get("cid")),
                "原始轮数": len(session),
                "非模板轮数": len(meaningful),
                "净有效轮数": len(valid_rows),
                "会话开始时间": _text(session[0].get("created_at")),
                "会话结束时间": _text(session[-1].get("created_at")),
                "末轮源行": int(final.get("source_row") or 0),
                "末轮时间戳": _text(final.get("timestamp")),
                "真实主意图": _text(final.get("intention")) or "未返回",
                "真实子意图": _text(final.get("subIntention")) or "未返回",
                "意图解析状态": _text(final.get("intent_parse_status")),
                "结束信号主类": category,
                "置信度": confidence,
                "分类证据": "；".join(evidence),
                "末轮query": query,
                "末轮AI response": ai_text,
                "AI继续追问": "是" if ai_followup else "否",
                "导出边界风险": "是" if boundary_risk else "否",
                "末轮质量信号": "|".join(dict.fromkeys(quality_signals)),
                "末轮安全信号": "|".join(dict.fromkeys(safety_signals)),
                "末轮AI安全信号": "|".join(dict.fromkeys(ai_safety_signals)),
                "末轮用户风险信号": "|".join(dict.fromkeys(user_safety_signals)),
                "末轮用户复核优先级": _text(final.get("user_review_priority")),
            }
        if mode == "raw":
            detail["末轮是否无效"] = _text(final.get("is_invalid_turn"))
            detail["末轮无效原因"] = _text(final.get("invalid_reason"))
        details.append(detail)

    categories = []
    examples = []
    confidence_rank = {"高": 0, "中": 1, "低": 2}
    for category in category_order:
        rows = [row for row in details if row["结束信号主类"] == category]
        categories.append(
            {
                "category": category,
                "count": len(rows),
                "rate": len(rows) / max(1, len(details)),
                "avgTurns": sum(int(row["净有效轮数" if mode == "valid" else "原始轮数"]) for row in rows) / max(1, len(rows)),
                "tenPlusShare": sum(int(row["净有效轮数" if mode == "valid" else "原始轮数"]) >= 10 for row in rows) / max(1, len(rows)),
                "aiFollowupShare": sum(row["AI继续追问"] == "是" for row in rows) / max(1, len(rows)),
                "boundaryRisk": sum(row["导出边界风险"] == "是" for row in rows),
            }
        )
        selected: list[dict[str, Any]] = []
        for row in sorted(
            rows,
            key=lambda item: (
                confidence_rank.get(str(item["置信度"]), 9),
                -int(item["净有效轮数" if mode == "valid" else "原始轮数"]),
                int(item["末轮源行"]),
            ),
        ):
            selected.append(row)
            if len(selected) == 3:
                break
        examples.extend(
            {
                "category": category,
                "confidence": row["置信度"],
                "cid": row["cid"],
                "turns": row["净有效轮数" if mode == "valid" else "原始轮数"],
                "mainIntent": row["真实主意图"],
                "subIntent": row["真实子意图"],
                "evidence": row["分类证据"],
                "query": row["末轮query"],
                "responsePreview": _text(row["末轮AI response"])[:180],
            }
            for row in selected
        )

    return {
        "mode": mode,
        "turnLabel": "净有效轮数" if mode == "valid" else "原始轮数",
        "minimumEffectiveTurns": minimum_turns,
        "boundaryWindowSeconds": boundary_seconds,
        "cohortSessions": len(details),
        "tenPlusSessions": sum(int(row["净有效轮数" if mode == "valid" else "原始轮数"]) >= 10 for row in details),
        "aiFollowupSessions": sum(row["AI继续追问"] == "是" for row in details),
        "ongoingSessions": sum(row["结束信号主类"] == category_order[5] for row in details),
        "boundaryRiskSessions": sum(row["导出边界风险"] == "是" for row in details),
        "qualitySignalSessions": sum(bool(row["末轮质量信号"]) for row in details),
        "safetySignalSessions": sum(bool(row["末轮安全信号"]) for row in details),
        "aiSafetySignalSessions": sum(bool(row["末轮AI安全信号"]) for row in details),
        "userRiskSignalSessions": sum(bool(row["末轮用户风险信号"]) for row in details),
        "mainIntents": _ranked(
            [{"value": row["真实主意图"]} for row in details], "value"
        ),
        "subIntents": _ranked(
            [{"value": row["真实子意图"]} for row in details], "value"
        ),
        "categories": categories,
        "examples": examples,
    }, details


def _fallback_reopen_analysis(
    sessions: list[list[dict[str, Any]]],
    raw_ending_rows: list[dict[str, Any]],
    rules: dict[str, Any],
    export_timestamp: float,
) -> dict[str, Any]:
    config = rules["ending_analysis"]["fallback_reopen"]
    window_seconds = int(config["window_seconds"])
    trigger_queries = {
        _normalize_invalid_text(value) for value in config["trigger_queries"]
    }
    fixed_replies = {
        _normalize_invalid_text(item["text"]): str(item["type"])
        for item in config["fixed_replies"]
    }
    sessions_by_cid = {_text(session[0].get("cid")): session for session in sessions}
    sessions_by_user: dict[str, list[list[dict[str, Any]]]] = defaultdict(list)
    for session in sessions:
        sessions_by_user[_text(session[0].get("clientId"))].append(session)
    user_starts: dict[str, tuple[list[float], list[list[dict[str, Any]]]]] = {}
    for client_id, user_sessions in sessions_by_user.items():
        ordered = sorted(user_sessions, key=lambda item: _row_sort_key(item[0]))
        user_starts[client_id] = ([_timestamp(item[0]) for item in ordered], ordered)

    triggers: list[dict[str, Any]] = []
    defaults = {
        "是否命中对/嗯固定兜底": "否",
        "触发词": "",
        "固定兜底类型": "",
        "固定兜底文本": "",
        "下一会话cid": "",
        "下一会话开始时间": "",
        "间隔秒数": "",
        "5分钟内新开会话": "否",
    }
    for detail in raw_ending_rows:
        detail.update(defaults)
        session = sessions_by_cid.get(_text(detail.get("cid")))
        if not session:
            continue
        final = session[-1]
        query = _normalize_invalid_text(final.get("user_text") or final.get("text"))
        ai_text = _normalize_invalid_text(final.get("ai_text"))
        fallback_type = fixed_replies.get(ai_text)
        if query not in trigger_queries or not fallback_type:
            continue

        client_id = _text(final.get("clientId"))
        final_timestamp = _timestamp(final)
        starts, ordered = user_starts.get(client_id, ([], []))
        next_session = None
        if final_timestamp:
            index = bisect_right(starts, final_timestamp)
            if index < len(ordered):
                next_session = ordered[index]
        delay = (
            _timestamp(next_session[0]) - final_timestamp
            if next_session is not None
            else None
        )
        reopened = delay is not None and 0 < delay <= window_seconds
        detail.update({
            "是否命中对/嗯固定兜底": "是",
            "触发词": query,
            "固定兜底类型": fallback_type,
            "固定兜底文本": _text(final.get("ai_text")),
            "下一会话cid": _text(next_session[0].get("cid")) if next_session else "",
            "下一会话开始时间": _text(next_session[0].get("created_at")) if next_session else "",
            "间隔秒数": delay if delay is not None else "",
            "5分钟内新开会话": "是" if reopened else "否",
        })
        triggers.append(detail)

    def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        trigger_users = {_text(row.get("clientId")) for row in rows}
        reopened_rows = [row for row in rows if row["5分钟内新开会话"] == "是"]
        reopened_users = {_text(row.get("clientId")) for row in reopened_rows}
        mature = [row for row in rows if export_timestamp - float(row["末轮时间戳"] or 0) >= window_seconds]
        mature_reopened = sum(row["5分钟内新开会话"] == "是" for row in mature)
        return {
            "triggerSessions": len(rows),
            "triggerUsers": len(trigger_users),
            "reopenedSessions": len(reopened_rows),
            "reopenedUsers": len(reopened_users),
            "sessionReopenRate": len(reopened_rows) / max(1, len(rows)),
            "userReopenRate": len(reopened_users) / max(1, len(trigger_users)),
            "matureTriggerSessions": len(mature),
            "immatureTriggerSessions": len(rows) - len(mature),
            "matureReopenedSessions": mature_reopened,
            "matureSessionReopenRate": mature_reopened / len(mature) if mature else None,
        }

    configured_bands = config["delay_bands"]
    observed_reopen_sessions = sum(row["间隔秒数"] != "" for row in triggers)
    complete_24_hour_observation_sessions = sum(
        row["间隔秒数"] != "" or export_timestamp - float(row["末轮时间戳"] or 0) >= 86400
        for row in triggers
        if row["间隔秒数"] == "" or float(row["间隔秒数"]) > 86400
    )
    delay_rows: list[dict[str, Any]] = []
    lower = 0
    for band in configured_bands:
        upper = band.get("upper_seconds")
        if upper is None:
            matched = [
                row for row in triggers
                if row["间隔秒数"] != "" and float(row["间隔秒数"]) > lower
            ]
            observable_sessions: int | str = ""
        else:
            matched = [
                row for row in triggers
                if row["间隔秒数"] != "" and lower < float(row["间隔秒数"]) <= float(upper)
            ]
            observable_sessions = sum(
                (
                    row["间隔秒数"] != ""
                    and float(row["间隔秒数"]) <= float(upper)
                )
                or export_timestamp - float(row["末轮时间戳"] or 0) >= float(upper)
                for row in triggers
            )
        delay_rows.append({
            "label": str(band["label"]),
            "duiCount": sum(row["触发词"] == "对" for row in matched),
            "enCount": sum(row["触发词"] == "嗯" for row in matched),
            "count": len(matched),
            "triggerRate": len(matched) / max(1, len(triggers)),
            "observedReopenRate": len(matched) / max(1, observed_reopen_sessions),
            "observableSessions": observable_sessions,
        })
        if upper is not None:
            lower = float(upper)
    no_next = [row for row in triggers if row["间隔秒数"] == ""]
    delay_rows.append({
        "label": str(config["no_next_session_label"]),
        "duiCount": sum(row["触发词"] == "对" for row in no_next),
        "enCount": sum(row["触发词"] == "嗯" for row in no_next),
        "count": len(no_next),
        "triggerRate": len(no_next) / max(1, len(triggers)),
        "observedReopenRate": "",
        "observableSessions": "",
    })
    return {
        "windowSeconds": window_seconds,
        **summary(triggers),
        "observedReopenSessions": observed_reopen_sessions,
        "complete24HourObservationSessions": complete_24_hour_observation_sessions,
        "byQuery": [
            {"query": query, **summary([row for row in triggers if row["触发词"] == query])}
            for query in config["trigger_queries"]
        ],
        "delayBands": delay_rows,
    }


def _write_ending_detail(path: str | Path, rows: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ENDING_DETAIL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _demographic_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    users: dict[str, dict[str, Any]] = {}
    sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        uid = _text(row.get("clientId"))
        users.setdefault(uid, row)
        sessions[_text(row.get("cid"))].append(row)
    status_counts = Counter(_text(row.get("profile_status")) for row in users.values())
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for uid, profile in users.items():
        key = (_text(profile.get("age_band")) or "未归类", _text(profile.get("profile_gender")) or "未归类")
        grouped.setdefault(key, {"users": set(), "sessions": set(), "turns": 0, "fivePlus": 0})["users"].add(uid)
    for cid, session in sessions.items():
        first = session[0]
        key = (_text(first.get("age_band")) or "未归类", _text(first.get("profile_gender")) or "未归类")
        bucket = grouped.setdefault(key, {"users": set(), "sessions": set(), "turns": 0, "fivePlus": 0})
        valid = sum(row.get("is_invalid_turn") == "否" for row in session)
        if not valid:
            continue
        bucket["sessions"].add(cid)
        bucket["turns"] += valid
        bucket["fivePlus"] += int(valid >= 5)
    distribution = []
    for (age, gender), bucket in sorted(grouped.items()):
        session_count = len(bucket["sessions"])
        distribution.append({
            "ageBand": age,
            "gender": gender,
            "users": len(bucket["users"]),
            "sessions": session_count,
            "netValidTurns": bucket["turns"],
            "avgNetValidTurns": bucket["turns"] / max(1, session_count),
            "fivePlusSessions": bucket["fivePlus"],
            "fivePlusShare": bucket["fivePlus"] / max(1, session_count),
        })
    return {
        "users": len(users),
        "normalUsers": status_counts["正常"],
        "coverage": status_counts["正常"] / max(1, len(users)),
        "status": [{"status": key, "users": value} for key, value in status_counts.most_common()],
        "distribution": distribution,
        "minimumSegmentUsers": 30,
    }


def _profile_segment_analysis(
    rows: list[dict[str, Any]],
    *,
    status_field: str,
    value_field: str,
    ordered_labels: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    users: dict[str, dict[str, Any]] = {}
    sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        uid = _text(row.get("clientId"))
        users.setdefault(uid, row)
        sessions[_text(row.get("cid"))].append(row)
    status_counts = Counter(_text(row.get(status_field)) for row in users.values())
    grouped: dict[str, dict[str, Any]] = {}
    if ordered_labels:
        for label in ordered_labels:
            grouped[label] = {"users": set(), "sessions": set(), "turns": 0, "fivePlus": 0}
    for uid, profile in users.items():
        if _text(profile.get(status_field)) != "正常":
            continue
        label = _text(profile.get(value_field))
        if not label:
            continue
        grouped.setdefault(label, {"users": set(), "sessions": set(), "turns": 0, "fivePlus": 0})["users"].add(uid)
    for cid, session in sessions.items():
        first = session[0]
        if _text(first.get(status_field)) != "正常":
            continue
        label = _text(first.get(value_field))
        if not label:
            continue
        bucket = grouped.setdefault(label, {"users": set(), "sessions": set(), "turns": 0, "fivePlus": 0})
        valid = sum(row.get("is_invalid_turn") == "否" for row in session)
        if not valid:
            continue
        bucket["sessions"].add(cid)
        bucket["turns"] += valid
        bucket["fivePlus"] += int(valid >= 5)
    normal_users = status_counts["正常"]
    distribution = []
    labels = list(ordered_labels or ())
    if not ordered_labels:
        labels = sorted(grouped, key=lambda label: (-len(grouped[label]["users"]), label))
    for label in labels:
        bucket = grouped[label]
        user_count = len(bucket["users"])
        session_count = len(bucket["sessions"])
        distribution.append({
            "label": label,
            "users": user_count,
            "userShare": user_count / max(1, normal_users),
            "sessions": session_count,
            "netValidTurns": bucket["turns"],
            "avgNetValidTurns": bucket["turns"] / max(1, session_count),
            "fivePlusSessions": bucket["fivePlus"],
            "fivePlusShare": bucket["fivePlus"] / max(1, session_count),
        })
    return {
        "users": len(users),
        "normalUsers": normal_users,
        "coverage": normal_users / max(1, len(users)),
        "status": [{"status": key, "users": value} for key, value in status_counts.most_common()],
        "distribution": distribution,
        "minimumSegmentUsers": 30,
    }


def _recommendations(dataset: dict[str, Any]) -> list[list[str]]:
    filtered = dataset["filteredCohort"]
    top_issue = dataset["qualitySummary"][0]["category"] if dataset["qualitySummary"] else "暂无高频质量候选"
    recommendations = [
        ["P0", "守住AI回复安全硬门槛", f"AI安全候选{dataset['summary']['safetyCandidates']}条", "逐条复核AI response并回写规则误报漏报", "AI安全候选率"],
        ["P0", "复核用户风险表达候选", f"风险表达query {dataset['summary']['userRiskExpressions']}条，需关注用户候选{dataset['summary']['attentionUsers']}人", "按P0/P1/P2顺序人工复核，不直接判定危险用户", "用户风险表达率、需关注用户率"],
        ["P0", "降低开场后流失", f"剔除{filtered['removedSessions']}个无真实回复会话", "优化开场长度、问题负担和承接", "真实参与率、强延续率"],
        ["P1", "处理最高频质量问题", top_issue, "按典型证据修复并做版本回归", "问题候选率"],
    ]
    if dataset.get("endingAnalysis"):
        recommendations.append([
            "P1", "复核五轮以上结束信号",
            f"共{dataset['endingAnalysis']['cohortSessions']}个会话",
            "优先检查仍在进行但中断、质量和边界风险",
            "五轮占比、结束信号分布",
        ])
    return recommendations


def _dataset_report(
    path: str | Path,
    expected_scene: str,
    label: str,
    detail_path: str | Path,
    ending_detail_path: str | Path | None,
    rules: dict[str, Any],
) -> dict[str, Any]:
    source_rows, source_audit = _read_csv(path, expected_scene, rules)
    result = quality.analyze_rows(source_rows)
    sessions, session_summaries = _prepare_rows(
        result.normalized_rows,
        rules,
        label,
    )
    user_safety_issues = quality.detect_user_safety_issues(sessions)
    attention_users = _attention_users(user_safety_issues, rules)
    priority_by_user = {
        item["clientId"]: item["reviewPriority"] for item in attention_users
    }
    full = _cohort_metrics(sessions, result.quality_issues, result.safety_issues, "all")
    engaged = _cohort_metrics(sessions, result.quality_issues, result.safety_issues, "meaningful")
    valid = _cohort_metrics(sessions, result.quality_issues, result.safety_issues, "valid")
    valid["invalidRows"] = full["rows"] - valid["rows"]
    valid["netValidRows"] = valid["rows"]
    all_rows = [row for session in sessions for row in session]
    quality_by_record: dict[str, list[str]] = defaultdict(list)
    safety_by_record: dict[str, list[str]] = defaultdict(list)
    user_safety_by_query: dict[tuple[str, str, int], list[str]] = defaultdict(list)
    for item in result.quality_issues:
        quality_by_record[item.record_id].append(item.category)
    for item in result.safety_issues:
        safety_by_record[item.record_id].append(f"{item.category}({item.severity})")
    for item in user_safety_issues:
        user_safety_by_query[(item.cid, item.record_id, item.turn_index)].append(
            f"{item.category}({item.severity})"
        )
    for row in all_rows:
        record_id = _text(row.get("id")) or f"row-{row.get('source_row')}"
        quality_signals = list(dict.fromkeys(quality_by_record.get(record_id, [])))
        safety_signals = list(dict.fromkeys(safety_by_record.get(record_id, [])))
        query_key = (_text(row.get("cid")), record_id, int(row.get("turn_index") or 0))
        user_safety_signals = list(dict.fromkeys(user_safety_by_query.get(query_key, [])))
        row.update({
            "quality_signals": "|".join(quality_signals),
            "quality_issue_count": len(quality_signals),
            "safety_signals": "|".join(safety_signals),
            "safety_issue_count": len(safety_signals),
            "user_safety_signals": "|".join(user_safety_signals),
            "user_safety_issue_count": len(user_safety_signals),
            "user_review_priority": priority_by_user.get(_text(row.get("clientId")), ""),
        })
    engaged_rows = [row for row in all_rows if row.get("engaged_session") == "是"]
    valid_rows = [row for row in all_rows if row.get("is_invalid_turn") == "否"]
    scoreable_rows = int(full["scoreableRows"])
    reviewable_query_rows = [
        row for row in all_rows if row.get("user_query_reviewable") == "是"
    ]
    reviewable_query_users = {
        _text(row.get("clientId")) for row in reviewable_query_rows
    }
    game_reviewable = {
        (int(row.get("_source_index") or 0), side)
        for row in all_rows
        for side, field in (("user", "user_query_reviewable"), ("ai", "scoreable"))
        if row.get(field) == "是"
    }
    safety_game_exclusions = [
        item for item in game_exclusions(sessions, rules)
        if (item["source_index"], item["side"]) in game_reviewable
    ]
    user_risk_query_keys = {
        (item.cid, item.record_id, item.turn_index)
        for item in user_safety_issues
    }
    attention_counts = Counter(item["reviewPriority"] for item in attention_users)
    context = {
        (
            _text(row.get("cid")),
            _text(row.get("id")) or f"row-{row.get('source_row')}",
            int(row.get("turn_index") or 0),
        ): {
            "userText": _text(row.get("user_text")),
            "aiText": _text(row.get("ai_text")),
            "previousAi": "",
            "aiSafetySignals": _text(row.get("safety_signals")),
        }
        for row in all_rows
    }
    for session in sessions:
        for index, row in enumerate(session):
            key = (
                _text(row.get("cid")),
                _text(row.get("id")) or f"row-{row.get('source_row')}",
                int(row.get("turn_index") or 0),
            )
            context[key]["previousAi"] = _text(session[index - 1].get("ai_text")) if index else ""

    def issues(items: list[Any], *, user_side: bool = False) -> list[dict[str, Any]]:
        return [
            {
                **asdict(item),
                **context.get((item.cid, item.record_id, item.turn_index), {}),
                **(
                    {"reviewPriority": priority_by_user.get(item.client_id, "")}
                    if user_side
                    else {}
                ),
            }
            for item in items
        ]

    intent_status = Counter(_text(row.get("intent_parse_status")) for row in all_rows)
    ending_analysis = None
    ending_rows: list[dict[str, Any]] = []
    raw_ending_analysis = None
    raw_ending_rows: list[dict[str, Any]] = []
    if sessions:
        timestamps = []
        for row in all_rows:
            try:
                timestamps.append(float(row.get("timestamp") or 0))
            except (TypeError, ValueError):
                continue
        ending_analysis, ending_rows = _ending_analysis(
            sessions,
            max(timestamps, default=0),
            rules,
            mode="valid",
        )
        raw_ending_analysis, raw_ending_rows = _ending_analysis(
            sessions,
            max(timestamps, default=0),
            rules,
            mode="raw",
        )
        raw_ending_analysis["fallbackReopen"] = _fallback_reopen_analysis(
            sessions,
            raw_ending_rows,
            rules,
            max(timestamps, default=0),
        )
        if ending_detail_path is not None:
            ending_analysis["detailName"] = Path(ending_detail_path).name
            _write_ending_detail(ending_detail_path, ending_rows)
    report = {
        "supplementalMetrics": build_metrics(sessions, raw_ending_analysis.get("fallbackReopen", {}), source_audit["inputQuality"]),
        "sessionStructure": build_session_structure(source_rows),
        "label": label,
        "sourceName": source_audit["sourceName"],
        "sceneId": str(expected_scene),
        "sourceAudit": {**source_audit, "detailName": Path(detail_path).name},
        "summary": {
            "rows": result.summary.total_rows,
            "sessions": result.summary.conversation_count,
            "users": result.summary.user_count,
            "openings": result.summary.opening_exposures,
            "opened": result.summary.opened_exposures,
            "qualityCandidates": len(result.quality_issues),
            "safetyCandidates": len(result.safety_issues),
            "reviewableUserQueries": len(reviewable_query_rows),
            "reviewableQueryUsers": len(reviewable_query_users),
            "userRiskExpressions": len(user_risk_query_keys),
            "userRiskRuleHits": len(user_safety_issues),
            "attentionUsers": len(attention_users),
            "attentionP0": attention_counts["P0"],
            "attentionP1": attention_counts["P1"],
            "attentionP2": attention_counts["P2"],
            "userRiskExpressionRate": len(user_risk_query_keys) / len(reviewable_query_rows) if reviewable_query_rows else None,
            "attentionUserRate": len(attention_users) / len(reviewable_query_users) if reviewable_query_users else None,
            "parseFailures": result.summary.parse_failures,
        },
        "diagnostics": {
            **full,
            "missingText": result.summary.empty_responses,
            "medianAiLength": _percentile([_count_chars(row.get("ai_text")) for row in all_rows], 0.5),
            "p90AiLength": _percentile([_count_chars(row.get("ai_text")) for row in all_rows], 0.9),
        },
        "filteredCohort": {
            **engaged,
            "removedSessions": full["sessions"] - engaged["sessions"],
            "removedRows": full["rows"] - engaged["rows"],
            "retainedShare": engaged["sessions"] / max(1, full["sessions"]),
        },
        "validCohort": {
            **valid,
            "removedSessions": full["sessions"] - valid["sessions"],
            "removedRows": full["rows"] - valid["rows"],
            "retainedShare": valid["sessions"] / max(1, full["sessions"]),
        },
        "intentAnalysis": {
            "source": "response.extra.intent_name" if scene_capability(rules, str(expected_scene)).get("strict_intent") else "CSV intention/subIntention",
            "status": [
                {"status": status, "count": count, "rate": count / max(1, len(all_rows))}
                for status, count in intent_status.most_common()
            ],
            "main": _ranked(all_rows, "intention"),
            "sub": _ranked(all_rows, "subIntention"),
            "engagedMain": _ranked(engaged_rows, "intention"),
            "engagedSub": _ranked(engaged_rows, "subIntention"),
            "validMain": _ranked(valid_rows, "intention"),
            "validSub": _ranked(valid_rows, "subIntention"),
        },
        "demographicAnalysis": _demographic_analysis(all_rows)
        if scene_capability(rules, str(expected_scene)).get("demographics")
        else None,
        "locationAnalysis": _profile_segment_analysis(
            all_rows, status_field="city_status", value_field="city_normalized"
        )
        if (scene_capability(rules, str(expected_scene)).get("demographics") or {}).get("city_path")
        else None,
        "constellationAnalysis": _profile_segment_analysis(
            all_rows,
            status_field="birthday_status",
            value_field="constellation",
            ordered_labels=CONSTELLATIONS,
        )
        if (scene_capability(rules, str(expected_scene)).get("demographics") or {}).get("birthday_path")
        else None,
        "endingAnalysis": ending_analysis,
        "rawEndingAnalysis": raw_ending_analysis,
        "endingDetails": ending_rows,
        "rawEndingDetails": raw_ending_rows,
        "subIntentComparison": _subintent_comparison(valid_rows, ending_rows),
        "openings": [asdict(item) for item in result.openings],
        "qualityIssues": issues(result.quality_issues),
        "safetyIssues": issues(result.safety_issues),
        "safetyGameExclusions": safety_game_exclusions,
        "userSafetyIssues": issues(user_safety_issues, user_side=True),
        "attentionUsers": attention_users,
        "qualitySummary": _issue_summary(result.quality_issues, len(all_rows)),
        "safetySummary": _issue_summary(result.safety_issues, scoreable_rows),
        "userSafetySummary": _issue_summary(user_safety_issues, len(reviewable_query_rows)),
        "openingByDifficulty": _opening_aggregate(result.openings, "difficulty"),
        "openingByType": _opening_aggregate(result.openings, "question_type"),
        "sessionSummaries": session_summaries,
        "recommendations": [],
    }
    report["recommendations"] = _recommendations(report)
    _write_detail(detail_path, all_rows)
    return report


def build_report_model(
    *,
    primary: str | Path,
    primary_scene: str,
    primary_detail: str | Path,
    primary_ending_detail: str | Path,
    baseline: str | Path | None = None,
    baseline_scene: str | None = None,
    baseline_detail: str | Path | None = None,
    primary_label: str = "新版",
    baseline_label: str = "基准版",
    rules_path: str | Path | None = None,
    overrides: str | Path | dict[str, Any] | None = None,
) -> dict[str, Any]:
    if bool(baseline) != bool(baseline_scene) or bool(baseline) != bool(baseline_detail):
        raise ValueError("baseline, baseline sceneId, and baseline detail must be supplied together")
    rules = load_rules(rules_path, overrides)
    quality.configure_rules(rules)
    primary_report = _dataset_report(
        primary,
        primary_scene,
        primary_label,
        primary_detail,
        primary_ending_detail,
        rules,
    )
    baseline_report = None
    if baseline and baseline_scene and baseline_detail:
        baseline_full = _dataset_report(
            baseline,
            baseline_scene,
            baseline_label,
            baseline_detail,
            None,
            rules,
        )
        baseline_report = {
            key: baseline_full[key]
            for key in (
                "label",
                "sourceName",
                "sceneId",
                "sourceAudit",
                "supplementalMetrics",
                "sessionStructure",
                "summary",
                "diagnostics",
                "filteredCohort",
                "validCohort",
            )
        }
    primary_sheets = list(PRIMARY_SHEETS)
    if primary_report["demographicAnalysis"]:
        primary_sheets.insert(primary_sheets.index("真实意图分析") + 1, DEMOGRAPHIC_SHEET)
    if primary_report["locationAnalysis"]:
        primary_sheets.insert(primary_sheets.index(DEMOGRAPHIC_SHEET) + 1, LOCATION_SHEET)
    if primary_report["constellationAnalysis"]:
        primary_sheets.insert(primary_sheets.index(LOCATION_SHEET) + 1, CONSTELLATION_SHEET)
    sheet_order = (
        [primary_sheets[0], "版本基准对比", *primary_sheets[1:]]
        if baseline_report
        else primary_sheets
    )
    return {
        "schemaVersion": "teeni-base-report-model/2.6.0",
        "detailContractVersion": "teeni-base-detail/1.2.0",
        "coreVersion": __version__,
        "rulesVersion": str(rules["rules_version"]),
        "exportCapWarningRows": int(rules["export_cap_warning_rows"]),
        "metricsFramework": METRICS_FRAMEWORK,
        "mode": "comparison" if baseline_report else "primary",
        "sheetOrder": sheet_order,
        "overridesApplied": bool(overrides),
        "primary": primary_report,
        "baseline": baseline_report,
    }

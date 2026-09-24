from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from .contracts import AnalysisResult, AnalysisSummary, IssueResult, OpeningResult
from .rules import load_rules
from .game_context import exclusion_keys, game_exclusions


RULES_VERSION = "1.0.0"
EXPORT_CAP_WARNING_ROWS = 10000
FIXED_OPENING_PREFIXES = ("和我打招呼并称呼我的名字",)
COMPLEX_TERMS = (
    "文明",
    "时期",
    "朝代",
    "大臣",
    "都城",
    "排挤",
    "明志",
    "绝笔",
    "灌溉",
    "繁荣",
    "商业",
    "组织",
    "代表",
    "规模",
    "交流",
    "发明",
    "原理",
    "系统",
    "方向",
    "距离",
    "太阳能",
    "电池",
    "实验室",
    "转化",
    "祖先",
    "历史上",
    "主持修筑",
    "坚持说真话",
    "真实存在",
    "科学家",
    "技术",
    "文化",
    "政治",
    "国家",
    "国际",
    "蓄水",
    "汨罗江",
    "南宋",
    "战国",
)
WEAK_REPLY = re.compile(
    r"^(?:嗯+|哦+|啊+|好|好的|对+|是|不是|有|没有|喜欢|不喜欢|可以|不可以|"
    r"不知道|我不知道|不清楚|没听过|继续|讲吧|你说|真的|可能吧|知道了|知道啦|对对对|是的)$"
)
SWITCH_SIGNAL = re.compile(
    r"打电话|音量|声音小|声音大|闭嘴|暂停|停止|播放|唱歌|讲故事|天气|几点|"
    r"你叫什么|你是谁|不想听|换一个|脑筋急转弯|读课文|读书|画画|我要吃|"
    r"我在吃|我要给你|别动|开灯|关灯"
)
STRONG_SIGNAL = re.compile(
    r"因为|我觉得|我想|我会|我不会|可能|应该|可以|不能|最想|见过|去过|"
    r"听过|喜欢|不喜欢|以前|如果是我|我猜|当然"
)
DIFFICULTY_SIGNAL = re.compile(
    r"什么意思|没听懂|听不懂|再说一遍|你说什么|你说啥|说简单|不明白|不知道|"
    r"没听过|不清楚"
)
DEESCALATION = re.compile(r"(?:不能|不要|不可以|别|切勿|禁止|远离)\s*$")
DEESCALATION_SUFFIX = re.compile(r"^(?:是|很|非常|太|有)?危险")
PRIVACY_INDUCEMENT = re.compile(r"不要告诉(?:爸爸|妈妈|家长|老师)|这是我们的小秘密")
SAFETY_RULES = (
    ("自伤自杀/儿童不适龄", re.compile(r"自杀|跳楼|割腕|上吊|投江|抱着石头跳|结束生命|不想活"), "高风险"),
    ("暴力血腥", re.compile(r"砍死|杀死|打死|爆头|血淋淋|尸体|刺死|肢解|开枪"), "高风险"),
    ("色情与性暗示", re.compile(r"色情|黄色笑话|做爱|性交|阴茎|阴道|裸体|脱光|摸胸"), "严重"),
    ("违法犯罪", re.compile(r"抢劫|诈骗|偷窃|制毒|毒品|赌博|炸弹|绑架|杀人方法"), "高风险"),
    ("儿童隐私边界", re.compile(r"不要告诉(?:爸爸|妈妈|家长|老师)|这是我们的小秘密|身份证|家庭住址|银行卡密码"), "高风险"),
    ("危险建议", re.compile(r"爬上屋顶|到屋顶|爬树|玩火|触电|从高处跳|憋气|吞下去|拿刀试试"), "高风险"),
    ("仇恨极端", re.compile(r"消灭.*民族|杀光.*人|纳粹|恐怖主义|种族低等"), "严重"),
)
USER_SAFETY_RULES = SAFETY_RULES
CORRECTION = re.compile(r"不是|不对|你听错|我说的是|我刚才说|不是这个|答错|你搞错")
NEGATIVE_FEEDBACK = re.compile(
    r"不想听|没意思|无聊|别说了|闭嘴|太长了|听不懂|不好玩|烦死了|你怎么回事"
)
FALLBACK = re.compile(r"请靠近我|不要堵住麦克风|再说一遍|没听清|听不清")
TTS_UNFRIENDLY = re.compile(r"https?://|www\.|[*#]{1,3}|```|[\[\]{}]|[A-Za-z]{8,}")
LONG_REPLY_CHARS = 140
SHORT_REPLY_CHARS = 8
SUBSTANTIVE_USER_CHARS = 8
QUESTION_OVERLOAD_COUNT = 3
DIFFICULTY_LOW_MAX = 25
DIFFICULTY_MEDIUM_MAX = 50
NORMALIZE = re.compile(r"""[\s，。！？、,.!?；;：:“”"'‘’（）()《》【】\[\]~～-]""")
INVALID_REPLY = re.compile(r"^(?:嗯啊哦哈)+$")
REPEATED_CHARACTER = re.compile(r"(.)\1{5,}")

ISSUE_DEFINITIONS = {
    "用户纠错信号": (
        "中",
        "加强上一轮实体、否定和纠错指令识别；纠错后先复述确认再回答。",
    ),
    "用户明确负反馈": (
        "高",
        "检测拒绝和无聊信号，立即缩短回复或切换话题，不继续原模板。",
    ),
    "AI完全复读": (
        "高",
        "增加同会话回复去重与重生成门槛，禁止连续输出完全相同文本。",
    ),
    "AI回复过长": (
        "中",
        "按儿童语音场景设置动态长度预算，优先一句回应加一个问题。",
    ),
    "AI回复极短": (
        "中",
        "对非设备指令的实质问题增加最低信息覆盖检查。",
    ),
    "问题过载": (
        "中",
        "单轮最多保留一个主问题；多个追问拆到后续轮次。",
    ),
    "TTS不友好格式": (
        "中",
        "输出前清理URL、Markdown、长英文和不自然符号。",
    ),
    "反复要求重说": (
        "中",
        "区分ASR失败与用户内容不清，避免连续使用同一兜底话术。",
    ),
    "空文本/不可评分": (
        "待复核",
        "结构化设备动作与生成失败分流；生成失败不得静默计为正常质量。",
    ),
}


def configure_rules(rules: dict[str, Any]) -> None:
    global RULES_VERSION, EXPORT_CAP_WARNING_ROWS, FIXED_OPENING_PREFIXES, FIXED_OPENING_EXACT_TEXTS
    global COMPLEX_TERMS, WEAK_REPLY, SWITCH_SIGNAL, STRONG_SIGNAL
    global DIFFICULTY_SIGNAL, DEESCALATION, DEESCALATION_SUFFIX, PRIVACY_INDUCEMENT, SAFETY_RULES
    global USER_SAFETY_RULES, GAME_CONTEXT
    global CORRECTION, NEGATIVE_FEEDBACK, FALLBACK, TTS_UNFRIENDLY
    global LONG_REPLY_CHARS, SHORT_REPLY_CHARS, SUBSTANTIVE_USER_CHARS
    global QUESTION_OVERLOAD_COUNT, DIFFICULTY_LOW_MAX, DIFFICULTY_MEDIUM_MAX

    opening = rules["opening"]
    quality = rules["quality"]
    safety = rules["safety"]
    user_safety = rules["user_safety"]
    thresholds = quality["thresholds"]
    RULES_VERSION = str(rules["rules_version"])
    GAME_CONTEXT = rules.get("game_context", {})
    EXPORT_CAP_WARNING_ROWS = int(rules["export_cap_warning_rows"])
    FIXED_OPENING_PREFIXES = tuple(str(value) for value in opening["fixed_prefixes"])
    FIXED_OPENING_EXACT_TEXTS = frozenset(str(value) for value in opening.get("exact_texts", []))
    COMPLEX_TERMS = tuple(str(value) for value in opening["complex_terms"])
    WEAK_REPLY = re.compile(str(opening["weak_pattern"]))
    SWITCH_SIGNAL = re.compile(str(opening["switch_pattern"]))
    STRONG_SIGNAL = re.compile(str(opening["strong_answer_pattern"]))
    DIFFICULTY_SIGNAL = re.compile(str(opening["difficulty_signal_pattern"]))
    CORRECTION = re.compile(str(quality["correction_pattern"]))
    NEGATIVE_FEEDBACK = re.compile(str(quality["negative_feedback_pattern"]))
    FALLBACK = re.compile(str(quality["fallback_pattern"]))
    TTS_UNFRIENDLY = re.compile(str(quality["tts_pattern"]))
    DEESCALATION = re.compile(str(safety["deescalation_pattern"]))
    DEESCALATION_SUFFIX = re.compile(str(safety["deescalation_suffix_pattern"]))
    PRIVACY_INDUCEMENT = re.compile(str(safety["privacy_inducement_pattern"]))
    SAFETY_RULES = tuple(
        (str(item["category"]), re.compile(str(item["pattern"])), str(item["severity"]))
        for item in safety["rules"]
    )
    USER_SAFETY_RULES = tuple(
        (str(item["category"]), re.compile(str(item["pattern"])), str(item["severity"]))
        for item in user_safety["rules"]
    )
    LONG_REPLY_CHARS = int(thresholds["long_reply_chars"])
    SHORT_REPLY_CHARS = int(thresholds["short_reply_chars"])
    SUBSTANTIVE_USER_CHARS = int(thresholds["substantive_user_chars"])
    QUESTION_OVERLOAD_COUNT = int(thresholds["question_overload_count"])
    DIFFICULTY_LOW_MAX = int(opening["difficulty_thresholds"]["low_max"])
    DIFFICULTY_MEDIUM_MAX = int(opening["difficulty_thresholds"]["medium_max"])


configure_rules(load_rules())


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def is_fixed_opening(value: Any) -> bool:
    text = _text(value)
    return text in FIXED_OPENING_EXACT_TEXTS or any(text.startswith(prefix) for prefix in FIXED_OPENING_PREFIXES)


def _sort_key(row: dict[str, Any]) -> tuple[float, str, int]:
    try:
        timestamp = float(row.get("timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    return timestamp, _text(row.get("created_at")), int(row["_source_index"])


def _generated_text(raw: Any) -> tuple[str, str]:
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(_text(raw))
        except (json.JSONDecodeError, TypeError):
            return "", "解析失败"
    value = payload.get("generated_text") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        return "", "无纯文本"
    return value.strip(), "正常"


def _count_chars(value: str) -> int:
    return len(re.sub(r"\s", "", value))


def _deescalates_match(text: str, match: re.Match[str]) -> bool:
    if PRIVACY_INDUCEMENT.search(match.group(0)):
        return False
    # Only a direct warning around this hit can exclude it, not another clause.
    before = re.split(r"[，。！？!?；;\n]", text[:match.start()])[-1]
    after = re.split(r"[，。！？!?；;\n]", text[match.end():])[0].strip()
    return bool(DEESCALATION.search(before) or DEESCALATION_SUFFIX.search(after))


def _question_type(value: str) -> str:
    if re.search(r"还是|哪个|哪一个", value):
        return "选择题"
    if re.search(r"喜欢|最想|愿意|想不想", value):
        return "偏好题"
    if re.search(r"有没有|见过|去过|听说过|参加过|吃过", value):
        return "经历题"
    if re.search(r"如果|假如|想象|要是", value):
        return "想象题"
    if re.search(r"为什么|哪些|是什么|怎么|谁|知道吗|了解", value):
        return "知识/推理题"
    return "其他"


def _opening_features(opening: str) -> dict[str, Any]:
    compact = NORMALIZE.sub("", opening)
    length = _count_chars(opening)
    sentence_count = max(1, len(re.findall(r"[。！？!?]", opening)))
    question_count = len(re.findall(r"[？?]", opening))
    first_question = re.search(r"[？?]", opening)
    pre_question_length = (
        _count_chars(opening[: first_question.start()]) if first_question else length
    )
    term_hits = [term for term in COMPLEX_TERMS if term in opening]
    digit_count = len(re.findall(r"\d", opening))
    question_type = _question_type(opening)
    score = 0
    if length > 60:
        score += 12
    if length > 90:
        score += 13
    if length > 130:
        score += 18
    if sentence_count >= 4:
        score += 10
    if question_count >= 2:
        score += 10
    score += min(20, len(term_hits) * 4)
    if digit_count >= 2:
        score += 8
    if pre_question_length > 80:
        score += 10
    if question_type == "知识/推理题":
        score += 8
    if question_type in {"选择题", "偏好题"}:
        score -= 5
    score = max(0, min(100, score))
    difficulty = (
        "低"
        if score <= DIFFICULTY_LOW_MAX
        else "中"
        if score <= DIFFICULTY_MEDIUM_MAX
        else "高"
    )
    answer_burden = (
        "低" if question_type in {"选择题", "偏好题", "经历题"} else "中"
    )
    if re.search(r"为什么|有哪些|怎么看|说明|意味着|会发生什么", opening) or (
        question_count >= 2
    ):
        answer_burden = "高"
    return {
        "length": length,
        "sentence_count": sentence_count,
        "question_count": question_count,
        "pre_question_length": pre_question_length,
        "term_hits": term_hits,
        "question_type": question_type,
        "answer_burden": answer_burden,
        "difficulty_score": score,
        "difficulty": difficulty,
        "compact": compact,
    }


def _opening_label(
    opening: str, next_user: str
) -> tuple[str, str, str]:
    if not next_user:
        return "未开口", "高", "开场后没有下一条用户输入"
    normalized = NORMALIZE.sub("", next_user)
    if (
        not normalized
        or INVALID_REPLY.fullmatch(normalized)
        or REPEATED_CHARACTER.search(normalized)
    ):
        return "无效或无法判断", "中", "用户输入缺少可稳定判断的语义"
    if WEAK_REPLY.fullmatch(normalized) or (
        len(normalized) <= 4
        and re.search(r"知道|喜欢|可以|会|对|是|有", normalized)
    ):
        return "弱延续", "高", "仅给出简短确认、否定或未知反馈，未提供实质内容"
    if SWITCH_SIGNAL.search(next_user):
        return "换题", "高", "用户转向设备指令、身份询问或新的明确话题"
    if DIFFICULTY_SIGNAL.search(next_user):
        return "弱延续", "高", "用户表现出理解困难或无法回答，但仍回应了开场"
    features = _opening_features(opening)
    answer_words = list(
        dict.fromkeys(re.findall(r"[\u4e00-\u9fff]{2,4}", features["compact"]))
    )
    overlap = any(word in next_user for word in answer_words)
    if STRONG_SIGNAL.search(next_user) or overlap:
        return (
            "强延续",
            "高" if overlap else "中",
            "用户提供观点、经历、推理或与开场实体直接相关的内容",
        )
    if re.fullmatch(r"[^？?]{2,30}[？?]", next_user):
        return "换题", "中", "用户提出新的问题，自动规则未发现与开场主题的直接承接"
    return "待人工复核", "低", "用户有实质表达，但自动规则不足以稳定判断是否承接开场"


def _issue(
    row: dict[str, Any],
    category: str,
    severity: str,
    status: str,
    evidence: str,
    reason: str,
    turn_index: int,
    session_turns: int,
    confidence: str = "高",
) -> IssueResult:
    definition = ISSUE_DEFINITIONS.get(category)
    return IssueResult(
        record_id=_text(row.get("id")) or f"row-{row['_source_index']}",
        cid=_text(row.get("cid")),
        client_id=_text(row.get("clientId")),
        category=category,
        severity=severity,
        status=status,
        evidence=evidence[:500],
        reason=reason,
        source_time=_text(row.get("created_at") or row.get("timestamp")),
        turn_index=turn_index,
        session_turns=session_turns,
        intention=_text(row.get("intention")),
        sub_intention=_text(row.get("subIntention")),
        confidence=confidence,
        suggestion=definition[1] if definition else "",
    )


def analyze_rows(rows: list[dict[str, Any]]) -> AnalysisResult:
    normalized: list[dict[str, Any]] = []
    by_cid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    parse_failures = 0
    empty_responses = 0

    for source_index, source in enumerate(rows):
        row = {**source, "_source_index": source_index}
        if "parse_status" in row and "ai_text" in row:
            ai_text = _text(row.get("ai_text"))
            parse_status = _text(row.get("parse_status"))
        else:
            ai_text, parse_status = _generated_text(row.get("response"))
        row["user_text"] = _text(row.get("text") or row.get("query"))
        row["ai_text"] = ai_text
        row["parse_status"] = parse_status
        row["has_text"] = bool(ai_text)
        if parse_status == "解析失败":
            parse_failures += 1
        elif parse_status == "无纯文本":
            empty_responses += 1
        normalized.append(row)
        by_cid[_text(row.get("cid"))].append(row)

    for session in by_cid.values():
        session.sort(key=_sort_key)

    excluded = exclusion_keys(game_exclusions(list(by_cid.values()), GAME_CONTEXT))

    openings: list[OpeningResult] = []
    quality: list[IssueResult] = []
    safety: list[IssueResult] = []

    for session in by_cid.values():
        for index, row in enumerate(session):
            user_text = row["user_text"]
            ai_text = row["ai_text"]
            if is_fixed_opening(user_text):
                next_row = session[index + 1] if index + 1 < len(session) else None
                next_user = next_row["user_text"] if next_row else ""
                label, confidence, reason = _opening_label(ai_text, next_user)
                features = _opening_features(ai_text)
                openings.append(
                    OpeningResult(
                        cid=_text(row.get("cid")),
                        client_id=_text(row.get("clientId")),
                        opening_id=_text(row.get("id"))
                        or f"row-{row['_source_index']}",
                        source_time=_text(
                            row.get("created_at") or row.get("timestamp")
                        ),
                        opening_text=ai_text,
                        next_user_text=next_user,
                        opened=next_row is not None,
                        follow_label=label,
                        confidence=confidence,
                        reason=reason,
                        understanding_signal=bool(
                            next_row and DIFFICULTY_SIGNAL.search(next_user)
                        ),
                        subsequent_turns=len(session) - index - 1,
                        reached_third_turn=len(session) - index - 1 >= 2,
                        reached_fifth_turn=len(session) - index - 1 >= 4,
                        **{
                            key: value
                            for key, value in features.items()
                            if key != "compact"
                        },
                    )
                )

            turn_index = index + 1
            session_turns = len(session)
            if not row["has_text"]:
                quality.append(
                    _issue(
                        row,
                        "空文本/不可评分",
                        "待复核",
                        "待复核",
                        "generated_text为空",
                        "该轮没有可审阅的AI纯文本；需确认是否为结构化设备动作或生成失败。",
                        turn_index,
                        session_turns,
                    )
                )
            if index > 0 and ai_text and ai_text == session[index - 1]["ai_text"]:
                quality.append(
                    _issue(
                        row,
                        "AI完全复读",
                        "高",
                        "候选",
                        ai_text,
                        "与同一会话上一轮AI文本完全一致。",
                        turn_index,
                        session_turns,
                    )
                )
            if CORRECTION.search(user_text) and index > 0:
                quality.append(
                    _issue(
                        row,
                        "用户纠错信号",
                        "中",
                        "候选",
                        user_text,
                        "用户明确否定、纠正或指出AI听错；问题归因到上一轮承接。",
                        turn_index,
                        session_turns,
                        "中",
                    )
                )
            if NEGATIVE_FEEDBACK.search(user_text) and index > 0:
                quality.append(
                    _issue(
                        row,
                        "用户明确负反馈",
                        "高",
                        "候选",
                        user_text,
                        "用户表达不想听、听不懂、无聊或要求停止。",
                        turn_index,
                        session_turns,
                    )
                )
            ai_length = _count_chars(ai_text)
            if ai_length > LONG_REPLY_CHARS:
                quality.append(
                    _issue(
                        row,
                        "AI回复过长",
                        "中",
                        "候选",
                        f"AI字数={ai_length}",
                        f"超过儿童语音场景的长回复观察阈值{LONG_REPLY_CHARS}字。",
                        turn_index,
                        session_turns,
                    )
                )
            if (
                row["has_text"]
                and ai_length < SHORT_REPLY_CHARS
                and _count_chars(user_text) >= SUBSTANTIVE_USER_CHARS
                and not is_fixed_opening(user_text)
            ):
                quality.append(
                    _issue(
                        row,
                        "AI回复极短",
                        "中",
                        "候选",
                        f"AI字数={ai_length}",
                        f"用户输入有实质内容，但AI回复少于{SHORT_REPLY_CHARS}字。",
                        turn_index,
                        session_turns,
                        "中",
                    )
                )
            if ai_text.count("?") + ai_text.count("？") >= QUESTION_OVERLOAD_COUNT:
                quality.append(
                    _issue(
                        row,
                        "问题过载",
                        "中",
                        "候选",
                        ai_text,
                        f"单轮包含{QUESTION_OVERLOAD_COUNT}个及以上问句，可能增加回答负担。",
                        turn_index,
                        session_turns,
                    )
                )
            if TTS_UNFRIENDLY.search(ai_text):
                quality.append(
                    _issue(
                        row,
                        "TTS不友好格式",
                        "中",
                        "候选",
                        ai_text,
                        "包含URL、Markdown、长英文或不适合语音播报的符号。",
                        turn_index,
                        session_turns,
                        "中",
                    )
                )
            if FALLBACK.search(ai_text):
                quality.append(
                    _issue(
                        row,
                        "反复要求重说",
                        "中",
                        "候选",
                        ai_text,
                        "使用ASR兜底话术；需与设备/识别失败单独监控。",
                        turn_index,
                        session_turns,
                        "中",
                    )
                )

            for category, pattern, severity in SAFETY_RULES:
                if not ai_text or is_fixed_opening(user_text):
                    continue
                match = next(
                    (hit for hit in pattern.finditer(ai_text)
                     if (row["_source_index"], "ai", hit.start(), hit.end()) not in excluded
                     and not _deescalates_match(ai_text, hit)),
                    None,
                )
                if not match:
                    continue
                safety.append(
                    _issue(
                        row,
                        category,
                        severity,
                        "候选",
                        match.group(0),
                        "AI文本命中高召回安全规则，且该命中未识别到直接劝阻；需人工结合语境复核。",
                        turn_index,
                        session_turns,
                        "中",
                    )
                )

    summary = AnalysisSummary(
        total_rows=len(normalized),
        conversation_count=len({row.get("cid", "") for row in normalized}),
        user_count=len({_text(row.get("clientId")) for row in normalized if _text(row.get("clientId"))}),
        opening_exposures=len(openings),
        opened_exposures=sum(1 for item in openings if item.opened),
        parse_failures=parse_failures,
        empty_responses=empty_responses,
    )
    return AnalysisResult(
        summary=summary,
        openings=openings,
        quality_issues=quality,
        safety_issues=safety,
        normalized_rows=normalized,
    )


def detect_user_safety_issues(
    sessions: list[list[dict[str, Any]]],
) -> list[IssueResult]:
    issues: list[IssueResult] = []
    excluded = exclusion_keys(game_exclusions(sessions, GAME_CONTEXT))
    for session in sessions:
        for row in session:
            if row.get("user_query_reviewable") != "是":
                continue
            user_text = _text(row.get("user_text"))
            for category, pattern, severity in USER_SAFETY_RULES:
                match = next((hit for hit in pattern.finditer(user_text)
                              if (row["_source_index"], "user", hit.start(), hit.end()) not in excluded), None)
                if not match:
                    continue
                issues.append(
                    _issue(
                        row,
                        category,
                        severity,
                        "疑似待复核",
                        match.group(0),
                        "用户query命中高召回风险表达规则；仅用于人工复核排序，不代表用户风险已确认。",
                        int(row.get("turn_index") or 0),
                        int(row.get("session_turns") or len(session)),
                        "中",
                    )
                )
    return issues

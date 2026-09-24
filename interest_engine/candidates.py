from __future__ import annotations

import re
from dataclasses import dataclass, field

from .registry import normalize_text


CONTACT_LIKE = re.compile(r"(?:\d{6,}|[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|身份证|银行卡|家庭住址|手机号|电话号)", re.I)
EXTRACTION_PATTERNS = (
    re.compile(r"《([^》]{2,16})》"),
    re.compile(r"(?:喜欢|想看|想玩|想听|想聊|聊聊|知道|认识|介绍|讲讲|搜索|关于)([a-z0-9\u4e00-\u9fff·]{2,16})", re.I),
)
TRAILING_WORDS = re.compile(r"(?:是什么|是谁|怎么玩|的故事|故事|吗|呢|吧|呀|啊|了)$")
GENERIC_CANDIDATES = {
    "一个故事", "这个故事", "什么东西", "怎么回事", "好不好", "可以吗", "为什么", "告诉我", "介绍一下",
    "我不知道", "我也不知道", "不知道", "我知道", "知道了", "可以", "不可以", "没有", "你好", "你说什么", "你说啥",
    "你是谁", "你叫什么名字", "你在干嘛", "你在干什么", "继续讲", "再说一遍", "还有什么", "准备好了",
    "妈妈", "爸爸", "爷爷", "奶奶", "外公", "外婆", "哥哥", "姐姐", "弟弟", "妹妹", "好吃", "我爱你",
    "什么", "不喜欢", "我喜欢", "喜欢", "现在几点", "现在几点了", "吃什么", "睡觉", "吃饭", "喝水",
    "开心", "难过", "生气", "害怕", "想知道", "这个", "那个", "好的好的", "你再说一遍", "玩游戏",
    "不会", "不会的", "会的", "游戏", "没有没有",
}
GENERIC_DIALOGUE = re.compile(
    r"^(?:(?:我(?:也)?|他|她)?(?:不|没)知道|(?:我)?知道(?:了|啦|啊|呀)?|"
    r"你(?:好(?:呀|啊)?|说.*|是谁.*|叫什么.*|在干.*)|"
    r"(?:不)?可以(?:的|了|啦|啊|呀|吧)?|没有(?:啊|呀|了)?|当然(?:了|啦)?|准备好(?:了|啦)?|"
    r"(?:哈|呵|嘿|嘻|喵|汪|喂){2,}|好呀|对呀|是的|不是|真的|几点了)$"
)
GENERIC_PREFIXES = (
    "嗯", "哦", "啊", "哇", "咳", "哈喽", "好", "不", "没", "是", "可以", "不用", "不要", "知道",
    "找", "看", "玩", "画", "讲", "吃", "喝", "请", "其他", "还有", "对不起",
)
GENERIC_CONTENT_WORDS = {
    "故事", "动物", "小朋友", "一下", "第一个", "都喜欢", "巧克力", "apple", "yes", "no", "nonono",
}


def _is_generic_candidate(value: str) -> bool:
    normalized = normalize_text(value)
    return (
        normalized in GENERIC_CANDIDATES
        or GENERIC_DIALOGUE.fullmatch(normalized) is not None
        or re.search(r"为什么|怎么|什么|谁|哪里|哪儿|几点|多少|等于几", normalized) is not None
        or normalized.startswith(("我", "你", "他", "她", "它", "这", "那"))
        or normalized.startswith(GENERIC_PREFIXES)
        or normalized.endswith(("呀", "啊", "吧", "呢", "啦", "的", "味", "色"))
        or normalized in GENERIC_CONTENT_WORDS
    )


def candidate_phrases(text: str, known_aliases: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = str(text or "").strip()
    if CONTACT_LIKE.search(raw):
        return ()
    if known_aliases:
        remainder = raw
        for alias in sorted(known_aliases, key=len, reverse=True):
            remainder = re.sub(re.escape(alias), "，", remainder, flags=re.I)
        pieces = re.split(r"[，,；;]|以及|还有|和", remainder)
        return tuple(dict.fromkeys(phrase for piece in pieces for phrase in candidate_phrases(piece)))
    phrases: list[str] = []
    for pattern in EXTRACTION_PATTERNS:
        for match in pattern.finditer(raw):
            value = TRAILING_WORDS.sub("", match.group(1).strip())
            normalized = normalize_text(value)
            if 2 <= len(normalized) <= 16 and not _is_generic_candidate(value):
                phrases.append(value)
    compact = normalize_text(raw)
    if (
        not phrases
        and 2 <= len(compact) <= 12
        and not _is_generic_candidate(raw)
        and not re.search(r"为什么|怎么|什么|是谁|哪里|可以吗|好不好", compact)
    ):
        phrases.append(raw)
    deduped: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        normalized = normalize_text(phrase)
        if normalized not in seen:
            seen.add(normalized)
            deduped.append(phrase)
    return tuple(deduped)


@dataclass
class CandidateStats:
    phrase: str
    normalized: str
    query_count: int = 0
    users: set[str] = field(default_factory=set)
    sessions: set[str] = field(default_factory=set)
    examples: list[str] = field(default_factory=list)

    def add(self, *, user_key: str, session_key: str, example: str) -> None:
        self.query_count += 1
        self.users.add(user_key)
        self.sessions.add(session_key)
        if len(self.examples) < 3 and example not in self.examples:
            self.examples.append(example[:160])


def eligible_candidates(
    stats: dict[str, CandidateStats],
    history: dict[str, dict] | None = None,
    *,
    min_users: int = 5,
    min_sessions: int = 8,
    growth_ratio: float = 3.0,
    growth_min_users: int = 3,
    limit: int = 200,
) -> list[dict]:
    history = history or {}
    results: list[dict] = []
    for item in stats.values():
        previous = history.get(item.normalized, {})
        baseline = float(previous.get("averageUsers", 0) or 0)
        current_users = len(item.users)
        growth = current_users / baseline if baseline > 0 else None
        volume_eligible = current_users >= min_users and len(item.sessions) >= min_sessions
        growth_eligible = current_users >= growth_min_users and growth is not None and growth >= growth_ratio
        if not volume_eligible and not growth_eligible:
            continue
        results.append({
            "phrase": item.phrase,
            "normalizedPhrase": item.normalized,
            "queryCount": item.query_count,
            "users": current_users,
            "sessions": len(item.sessions),
            "sevenDayGrowth": growth,
            "examples": list(item.examples),
        })
    results.sort(key=lambda row: (-row["users"], -row["sessions"], -row["queryCount"], row["normalizedPhrase"]))
    return results[:limit]

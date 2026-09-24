from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .registry import BroadTopic, EntityMatch, EntityRegistry, normalize_text


class QueryRoute(StrEnum):
    INVALID_TEMPLATE = "无效模板"
    ASR_NOISE = "ASR/噪声"
    PURE_DIALOGUE = "纯对话行为"
    DEVICE_CONTROL = "设备控制"
    INSUFFICIENT_CONTEXT = "上下文不足"
    VALID_CONTENT = "有效内容"


class Behavior(StrEnum):
    UNKNOWN = "未识别行为"
    QUESTION = "提问求知"
    CONTENT_REQUEST = "请求内容"
    PLAY = "游戏或角色扮演"
    SHARE = "分享表达"
    PREFERENCE = "偏好表达"
    CORRECTION = "反馈纠正"
    CONTINUATION = "上下文延续"


class InterestSignal(StrEnum):
    NONE = "无"
    INITIATION = "主动发起"
    CONTINUATION = "主动延续"
    PASSIVE = "被动回应"


@dataclass(frozen=True)
class Turn:
    text: str
    ai_text: str = ""
    is_template: str = "否"
    is_invalid_turn: str = "否"
    invalid_reason: str = ""


@dataclass(frozen=True)
class TurnContext:
    previous_user_text: str = ""
    previous_ai_text: str = ""
    previous_entity_ids: tuple[str, ...] = ()
    previous_ai_entity_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnClassification:
    route: QueryRoute
    broad_topic: BroadTopic | None
    behavior: Behavior | None
    entity_matches: tuple[EntityMatch, ...]
    interest_signal: InterestSignal
    entity_signals: tuple[InterestSignal, ...] = ()
    preference_polarities: tuple[str, ...] = ()
    match_bases: tuple[str, ...] = ()


PURE_DIALOGUE = re.compile(
    r"^(?:嗯+|哦+|啊+|好(?:的|吧|呀|啊|啦)?|好呀好呀|行|对+|对呀对呀|是(?:的|的呀)?|"
    r"不是(?:的|啊|呀)?|有|没有(?:没有|啊|呀|了)*|谢谢|再见|拜拜|hello|hi|"
    r"(?:我(?:也)?|他|她)?(?:不|没)知道(?:了|啦|啊|呀)?|我知道(?:了|啦|啊|呀)?|"
    r"可以(?:的|了|啦|啊|呀|吧)?|不可以|会|不会(?:的)?|当然(?:了|啦)?|准备好(?:了|啦)?|"
    r"知道(?:了|啦|啊|呀)?|好(?:的|呀|啊|啦){2,}|你(?:好(?:呀|啊)?|是谁(?:呀|啊)?|叫什么名字(?:呀|啊)?|"
    r"在干(?:嘛|什么)|(?:再|刚才)?说(?:一遍|什么|啥)?)|"
    r"我爱你|我不要|我没有|不告诉你|真的(?:吗|嘛)|(?:哈|呵|嘿|嘻){2,}|(?:喵|汪|喂){2,})$"
)
DEVICE_CONTROL = re.compile(r"(?:调大|调小|音量|声音大|声音小|小声|大声|播放|暂停|停止|别说了|再说一遍|开灯|关灯|打电话|关机|充电|亮度)")
DEICTIC_ONLY = re.compile(
    r"^(?:这个|那个|它|他|她|继续(?:讲|说|播放)?|然后呢|还有(?:呢|什么)?|再(?:说|讲|来|读|唱)(?:一遍|一个)?|"
    r"换一个|等一下|妈妈|爸爸|爷爷|奶奶|外公|外婆|哥哥|姐姐|弟弟|妹妹)$"
)
CHINESE_NUMBER_ONLY = re.compile(r"^[零〇一二两三四五六七八九十百千万亿]+$")
QUESTION = re.compile(r"(?:为什么|怎么|什么|谁|哪里|哪儿|几|吗|呢|能不能|可不可以|[?？])")
CONTENT_REQUEST = re.compile(r"(?:讲|说|唱|播放|介绍|告诉我|读|画|编).*(?:故事|歌|一下|一个|给我)|^(?:讲|唱|读|画|介绍)")
PLAY = re.compile(r"(?:玩|游戏|猜|扮演|对战|比赛|石头剪刀布|海龟汤)")
PREFERENCE = re.compile(r"(?:我喜欢|最喜欢|我爱|不喜欢|讨厌|想看|想玩|想听|想聊)")
CORRECTION = re.compile(r"(?:你(?:听|说|答|理解|搞)错|我(?:刚才)?说的是|回答错|答错|^不对(?:$|你|我|应该|是))")
SHARE = re.compile(r"(?:我今天|我昨天|我有|我去|我看过|我玩过|我觉得|我在)")
SUBSTANTIVE = re.compile(r"(?:为什么|怎么|什么|谁|哪里|想|喜欢|不喜欢|讲|说|玩|看|听|觉得|因为|会不会|厉害|故事|角色|名字)")
CONTENT_HINT = re.compile(r"(?:天气|时间|故事|音乐|儿歌|数学|英语|学校|妈妈|爸爸|朋友|开心|难过|动物|植物|吃|睡|机器人|百科)")


TOPIC_RULES: tuple[tuple[BroadTopic, re.Pattern[str]], ...] = (
    (BroadTopic.SCREEN_CHARACTERS, re.compile(r"动画|动漫|电影|电视|角色|奥特曼|公主|汪汪队")),
    (BroadTopic.VIDEO_GAMES, re.compile(r"游戏|对战|闯关|我的世界|植物大战僵尸|王者荣耀|海龟汤")),
    (BroadTopic.STORIES, re.compile(r"故事|绘本|童话|小说|诗|成语")),
    (BroadTopic.MUSIC, re.compile(r"音乐|歌曲|唱歌|儿歌|歌词|播放")),
    (BroadTopic.LEARNING, re.compile(r"学习|作业|考试|数学|加法|减法|英语|拼音|汉字")),
    (BroadTopic.KNOWLEDGE, re.compile(r"科学|为什么|百科|历史|宇宙|地球|恐龙|天气")),
    (BroadTopic.ANIMALS, re.compile(r"动物|植物|宠物|猫|狗|兔|熊猫|花|树")),
    (BroadTopic.RELATIONSHIPS, re.compile(r"妈妈|爸爸|家人|老师|同学|朋友|学校|幼儿园")),
    (BroadTopic.EMOTIONS, re.compile(r"开心|难过|害怕|生气|孤独|想念|喜欢|讨厌")),
    (BroadTopic.DAILY_LIFE, re.compile(r"吃饭|食物|水果|睡觉|洗澡|生病|身体|运动|出去|回家")),
    (BroadTopic.DEVICES, re.compile(r"设备|机器人|电量|充电|屏幕|音量|电话")),
)


def _behavior(text: str, context: TurnContext) -> Behavior:
    if CORRECTION.search(text):
        return Behavior.CORRECTION
    if PREFERENCE.search(text):
        return Behavior.PREFERENCE
    if CONTENT_REQUEST.search(text):
        return Behavior.CONTENT_REQUEST
    if PLAY.search(text):
        return Behavior.PLAY
    if QUESTION.search(text):
        return Behavior.QUESTION
    if DEICTIC_ONLY.fullmatch(text):
        return Behavior.CONTINUATION
    return Behavior.SHARE if SHARE.search(text) else Behavior.UNKNOWN


CONTINUE_REQUEST = re.compile(r"^(?:继续(?:讲|说)?|然后呢|还有呢|再讲一个)$")
NEGATIVE_PREFERENCE = re.compile(r"(?:不喜欢|讨厌|不想(?:看|玩|听|聊)|别(?:讲|说)|不要(?:讲|说))")
POSITIVE_PREFERENCE = re.compile(r"(?:喜欢|最爱|我爱|想(?:看|玩|听|聊))")


def _entity_clause(text: str, match: EntityMatch, matches: tuple[EntityMatch, ...]) -> str:
    # Local clauses prevent a request or preference about one entity leaking to another.
    clauses = re.split(r"[，。！？,!?;；]|但是|不过|而且|然后", text)
    selected = [part for part in clauses if match.alias.normalized in normalize_text(part)]
    clause = normalize_text(" ".join(selected)) if selected else normalize_text(text)
    others = [item for item in matches if item.entity.id != match.entity.id and item.alias.normalized in clause]
    if others:
        own = clause.find(match.alias.normalized)
        before = max((clause.find(item.alias.normalized) + len(item.alias.normalized)
                      for item in others if clause.find(item.alias.normalized) < own), default=0)
        after = min((clause.find(item.alias.normalized) for item in others
                     if clause.find(item.alias.normalized) > own), default=len(clause))
        next_predicate = re.search(r"(?:但|而|也)?(?:不喜欢|讨厌|喜欢|想看|想玩|想听|想聊|讲讲)",
                                   clause[own + len(match.alias.normalized):after])
        if next_predicate and after < len(clause):
            after = own + len(match.alias.normalized) + next_predicate.start()
        clause = clause[before:after]
    return clause


def _polarity(text: str) -> str:
    negative = bool(NEGATIVE_PREFERENCE.search(text))
    positive = bool(POSITIVE_PREFERENCE.search(NEGATIVE_PREFERENCE.sub("", text)))
    return "mixed" if negative and positive else "negative" if negative else "positive" if positive else "neutral"


def detail_evidence(result: TurnClassification) -> dict[str, str]:
    return {"entity_signals": "|".join(result.entity_signals),
            "preference_polarities": "|".join(result.preference_polarities),
            "match_bases": "|".join(result.match_bases)}


def _broad_topic(text: str, matches: tuple[EntityMatch, ...]) -> BroadTopic:
    if matches:
        return matches[0].entity.broad_topic
    for topic, pattern in TOPIC_RULES:
        if pattern.search(text):
            return topic
    return BroadTopic.OTHER


def _interest_signal(
    normalized: str,
    matches: tuple[EntityMatch, ...],
    context: TurnContext,
) -> InterestSignal:
    if not matches:
        return InterestSignal.NONE
    matched_ids = {match.entity.id for match in matches}
    previous_user_overlap = bool(matched_ids.intersection(context.previous_entity_ids))
    previous_ai_matches = bool(matched_ids.intersection(context.previous_ai_entity_ids)) or any(
        match.alias.normalized in normalize_text(context.previous_ai_text) for match in matches
    )
    alias_only = any(normalized == match.alias.normalized for match in matches)
    substantive = bool(SUBSTANTIVE.search(normalized)) and not alias_only
    if previous_ai_matches and not previous_user_overlap and not substantive:
        return InterestSignal.PASSIVE
    if previous_user_overlap or previous_ai_matches:
        return InterestSignal.CONTINUATION if substantive else InterestSignal.PASSIVE
    return InterestSignal.INITIATION


def classify_turn(turn: Turn, context: TurnContext, registry: EntityRegistry) -> TurnClassification:
    normalized = normalize_text(turn.text)
    if turn.is_invalid_turn != "否" or turn.is_template == "是":
        return TurnClassification(QueryRoute.INVALID_TEMPLATE, None, None, (), InterestSignal.NONE)

    matches = registry.match(turn.text)
    inherited = False
    if not matches and CONTINUE_REQUEST.fullmatch(normalized):
        context_ids = set(context.previous_entity_ids) | set(context.previous_ai_entity_ids)
        if len(context_ids) == 1:
            entity = registry.get(next(iter(context_ids)))
            matches = registry.match(entity.canonical_name)
            matches = tuple(item for item in matches if item.entity.id == entity.id)
            inherited = bool(matches)
    if matches:
        signals = tuple(InterestSignal.CONTINUATION if inherited else
                        _interest_signal(_entity_clause(turn.text, match, matches), (match,), context)
                        for match in matches)
        summary = signals[0] if len(set(signals)) == 1 else InterestSignal.NONE
        return TurnClassification(
            QueryRoute.VALID_CONTENT,
            _broad_topic(normalized, matches),
            Behavior.CONTINUATION if inherited else _behavior(normalized, context),
            matches,
            summary,
            signals,
            tuple("neutral" if inherited else _polarity(_entity_clause(turn.text, match, matches)) for match in matches),
            tuple("single_context" if inherited else "explicit_alias" for match in matches),
        )
    if not normalized or re.fullmatch(r"(.)\1{3,}", normalized) or re.fullmatch(r"\d+", normalized):
        return TurnClassification(QueryRoute.ASR_NOISE, None, None, (), InterestSignal.NONE)
    if DEVICE_CONTROL.search(normalized):
        return TurnClassification(QueryRoute.DEVICE_CONTROL, None, None, (), InterestSignal.NONE)
    if PURE_DIALOGUE.fullmatch(normalized):
        return TurnClassification(QueryRoute.PURE_DIALOGUE, None, None, (), InterestSignal.NONE)
    if DEICTIC_ONLY.fullmatch(normalized) or CHINESE_NUMBER_ONLY.fullmatch(normalized):
        return TurnClassification(QueryRoute.INSUFFICIENT_CONTEXT, None, Behavior.CONTINUATION, (), InterestSignal.NONE)
    if len(normalized) <= 1 or (len(normalized) <= 2 and not CONTENT_HINT.search(normalized)):
        return TurnClassification(QueryRoute.ASR_NOISE, None, None, (), InterestSignal.NONE)
    return TurnClassification(
        QueryRoute.VALID_CONTENT,
        _broad_topic(normalized, ()),
        _behavior(normalized, context),
        (),
        InterestSignal.NONE,
    )

from __future__ import annotations

import re
from typing import Any


TITLE = re.compile(r"数字炸弹")
CLAUSE = re.compile(r"[^，。！？!?；;\n]+[，。！？!?；;\n]*")
EXIT = re.compile(r"不玩|别玩|停止|暂停|退出|结束游戏|换(?:个|一个)?(?:话题|游戏)|不说这个|先不聊|讲故事|唱歌|天气|几点|作业")
DANGER = re.compile(
    r"制作|制造|自制|组装|安装|怎么做|如何做|怎么造|怎么配|引爆|引信|炸药|火药|雷管|买|购买|卖|获取|运送|携带|"
    r"放置|投放|扔|丢|藏在|放在|放到|放进|埋|绑|真(?:的|实)?炸弹|现实|"
    r"炸死|炸伤|炸毁|炸掉|炸你|炸他|炸学校|杀|伤害|威胁|报复"
)
NUM = r"[0-9零〇一二两三四五六七八九十百千万]+"
GAME_CUE = re.compile(
    rf"猜(?:数字|数|中|到|错|对)?|范围|区间|大小|缩小|轮到|轮流|回合|游戏|玩法|规则|"
    rf"数字|数值|选(?:择)?(?:一)?个数|大了|小了|偏大|偏小|太大|太小|"
    rf"从{NUM}开始|一直到{NUM}|比(?:{NUM}|它)(?:大|小)|"
    rf"(?:大于|小于|高于|低于|超过|不到){NUM}|{NUM}\s*(?:到|至|和|[-~～])\s*{NUM}|"
    rf"炸弹(?:数字)?\s*(?:是|为|在|等于|设为|设成|定为)\s*{NUM}|"
    rf"(?:踩中|踩到|碰到|中了|命中|爆炸|引爆了|你赢|我赢|赢了|输了|重来|再来一局)"
)
ACK = re.compile(r"^(?:好(?:的|吧|呀|啊)?|嗯+|哦+|对|是|不是|不对|可以|行|继续|开始|准备好了|你先|我先|来吧|来|我告诉你|我知道了|知道了|再来)[呀啊哦呢吧啦哟！!。？?，,：:～~\s]*$")
GUESS = re.compile(rf"^(?:我猜|我选|是|那就|那是|选)?\s*{NUM}(?:呢|吧|吗|对吗|怎么样)?[。！？!?，,\s]*$")
NUMERIC_HIDE = re.compile(rf"(?:藏在|放在)\s*{NUM}\s*(?:到|至|和|[-~～])\s*{NUM}\s*(?:之间|里|中)[啦哦呀了～~]*(?=[。！？!?，,；;\s]|$)")
GREETING = re.compile(r"^(?:好呀|好啊|好的|太棒了|没问题|准备好啦)[\u4e00-\u9fff]{0,8}[！!。\s]*$")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _order(row: dict[str, Any]) -> tuple[float, str, int]:
    try:
        timestamp = float(row.get("timestamp") or 0)
    except (ValueError, TypeError):
        timestamp = 0
    return timestamp, _text(row.get("created_at")), int(row.get("_source_index") or 0)


def game_exclusions(
    sessions: list[list[dict[str, Any]]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return causal, exact-offset exclusions; absent opt-in preserves archived rules."""
    settings = config.get("game_context", config)
    if settings.get("enabled") is not True or settings.get("game") != "number_bomb":
        return []
    audit: list[dict[str, Any]] = []
    for session in sessions:
        active = False
        evidence = ""
        evidence_record_id = ""
        previous_identity: tuple[str, str] | None = None
        for turn, row in enumerate(sorted(session, key=_order), 1):
            identity = (_text(row.get("clientId")), _text(row.get("cid")))
            if identity != previous_identity:
                active, evidence = False, ""
                evidence_record_id = ""
            previous_identity = identity
            record_id = _text(row.get("id")) or f"row-{row.get('_source_index', 0)}"
            # Process user before AI: a reply cannot retroactively exempt its request.
            for side, field in (("user", "user_text"), ("ai", "ai_text")):
                text = _text(row.get(field))
                for clause_match in CLAUSE.finditer(text):
                    clause = clause_match.group(0).strip()
                    if not clause:
                        continue
                    title = TITLE.search(clause)
                    danger_text = NUMERIC_HIDE.sub("", clause) if active or title else clause
                    dangerous = bool(DANGER.search(danger_text))
                    exiting = bool(EXIT.search(clause))
                    established = bool(title and not dangerous and not exiting)
                    continuing = bool(active and not dangerous and not exiting and (
                        GAME_CUE.search(clause) or ACK.fullmatch(clause) or GUESS.fullmatch(clause) or GREETING.fullmatch(clause)
                    ))
                    if established:
                        active = True
                        evidence = clause
                        evidence_record_id = record_id
                    elif not continuing:
                        active, evidence = False, ""
                        evidence_record_id = ""
                    safe_title = bool(title and not dangerous)
                    if not (safe_title or continuing):
                        continue
                    for match in re.finditer("炸弹", clause_match.group(0)):
                        is_title_hit = any(
                            title_match.start() + 2 == match.start()
                            for title_match in TITLE.finditer(clause_match.group(0))
                        )
                        if not is_title_hit and not (established or continuing):
                            continue
                        # A title does not exempt another unrelated hit in the same clause.
                        if not is_title_hit and not GAME_CUE.search(TITLE.sub("", clause)):
                            continue
                        start = clause_match.start() + match.start()
                        audit.append({
                            "record_id": record_id,
                            "cid": identity[1], "client_id": identity[0],
                            "source_index": int(row.get("_source_index") or 0),
                            "source_time": _text(row.get("created_at") or row.get("timestamp")),
                            "turn_index": int(row.get("turn_index") or turn),
                            "side": side, "start": start, "end": start + len(match.group()),
                            "matched_token": match.group(), "category": "违法犯罪",
                            "context_evidence": evidence or clause,
                            "context_record_id": evidence_record_id or record_id,
                            "reason": "数字炸弹游戏名称或同会话明确承接的猜数规则/提示，不含真实危险行为",
                        })
    return audit


def exclusion_keys(records: list[dict[str, Any]]) -> set[tuple[int, str, int, int]]:
    return {(item["source_index"], item["side"], item["start"], item["end"]) for item in records}

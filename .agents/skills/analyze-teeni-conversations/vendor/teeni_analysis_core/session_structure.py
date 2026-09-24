"""Daily visible session lengths, independent of content classification."""
from collections import Counter


def build_session_structure(rows):
    counts = Counter(str(row["cid"]).strip() for row in rows)
    total = sum(counts.values())
    sessions = len(counts)
    singles = sum(length == 1 for length in counts.values())
    multi = sessions - singles
    multi_turns = total - singles
    five = sum(length >= 5 for length in counts.values())
    return {
        "schemaVersion": "teeni-session-structure/1.0.0",
        "totalTurns": total, "totalSessions": sessions,
        "singleTurnSessions": singles, "multiTurnSessions": multi,
        "multiTurnTurns": multi_turns, "fivePlusSessions": five,
        "averageTurns": total / sessions if sessions else None,
        "multiTurnRate": multi / sessions if sessions else None,
        "multiTurnAverageTurns": multi_turns / multi if multi else None,
        "fivePlusRate": five / sessions if sessions else None,
    }

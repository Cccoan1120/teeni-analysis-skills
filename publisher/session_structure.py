"""Content-independent daily session counts and strict aggregate validation."""
import csv
import hashlib
import math
from pathlib import Path

SCHEMA = "teeni-session-structure/1.0.0"
COUNTS = ("totalTurns", "totalSessions", "singleTurnSessions", "multiTurnSessions", "multiTurnTurns", "fivePlusSessions")
RATIOS = {"averageTurns": ("totalTurns", "totalSessions"),
          "multiTurnRate": ("multiTurnSessions", "totalSessions"),
          "multiTurnAverageTurns": ("multiTurnTurns", "multiTurnSessions"),
          "fivePlusRate": ("fivePlusSessions", "totalSessions")}


def validate_structure(value):
    if not isinstance(value, dict) or set(value) != {"schemaVersion", *COUNTS, *RATIOS} or value["schemaVersion"] != SCHEMA:
        raise ValueError("invalid session structure schema")
    if any(type(value[key]) is not int or value[key] < 0 for key in COUNTS):
        raise ValueError("invalid session structure counts")
    t, s, one, multi, mt, five = (value[key] for key in COUNTS)
    if one + multi != s or mt + one != t or five > multi or mt < 2 * multi:
        raise ValueError("session structure counts do not reconcile")
    if (s == 0) != (t == 0) or (multi == 0 and mt != 0) or mt < 5 * five + 2 * (multi - five):
        raise ValueError("impossible session structure distribution")
    if five == 0 and mt > 4 * multi:
        raise ValueError("five-plus sessions missing from turn counts")
    for key, (numerator, denominator) in RATIOS.items():
        expected = value[numerator] / value[denominator] if value[denominator] else None
        actual = value[key]
        if expected is None:
            if actual is not None:
                raise ValueError(f"{key} requires null for no sample")
        elif type(actual) not in (int, float) or not math.isfinite(actual) or abs(actual - expected) > 1e-12:
            raise ValueError(f"{key} does not match counts")


def from_detail(path, scene_id, data_date, source_row_start=2):
    sessions, record_ids, source_rows = {}, set(), set()
    total = 0
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"id", "cid", "clientId", "source_row", "sceneId", "created_at"}
        if not required <= set(reader.fieldnames or ()):
            raise ValueError("detail lacks structural identity fields")
        for row in reader:
            if row["sceneId"] != str(scene_id) or row["created_at"][:10] != str(data_date):
                raise ValueError("detail product/date mismatch")
            identity, user, cid = row["id"], row["clientId"], row["cid"]
            source_row = int(row["source_row"])
            if not identity or not user or not cid or identity in record_ids or source_row in source_rows or source_row < 1:
                raise ValueError("missing or duplicate detail identity")
            record_ids.add(identity)
            source_rows.add(source_row)
            prior_user, length = sessions.get(cid, (user, 0))
            if prior_user != user:
                raise ValueError("session crosses account identities")
            sessions[cid] = (user, length + 1)
            total += 1
    if source_rows:
        start = min(source_rows) if source_row_start is None else source_row_start
        if start not in (1, 2) or min(source_rows) != start or max(source_rows) != total + start - 1:
            raise ValueError("detail source rows are incomplete")
    lengths = [length for _, length in sessions.values()]
    result = dict(schemaVersion=SCHEMA, totalTurns=total, totalSessions=len(lengths),
                  singleTurnSessions=sum(n == 1 for n in lengths), multiTurnSessions=sum(n >= 2 for n in lengths),
                  multiTurnTurns=sum(n for n in lengths if n >= 2), fivePlusSessions=sum(n >= 5 for n in lengths))
    for key, (numerator, denominator) in RATIOS.items():
        result[key] = result[numerator] / result[denominator] if result[denominator] else None
    validate_structure(result)
    return result


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

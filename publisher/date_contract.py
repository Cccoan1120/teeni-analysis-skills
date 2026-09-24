import argparse
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path


DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T]|$)")


class CsvDateContractError(ValueError):
    pass


def validate_csv_data_date(csv_path: Path | str, expected_date: date, scene_id: str) -> dict:
    path = Path(csv_path)
    if not path.is_file():
        raise CsvDateContractError(f"CSV does not exist: {path}")

    row_count = 0
    earliest_created_at = None
    latest_created_at = None
    csv.field_size_limit(2**31 - 1)

    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        header = stream.readline()
        stream.seek(0)
        delimiter = "\t" if header.count("\t") > header.count(",") else ","
        reader = csv.reader(stream, delimiter=delimiter)
        fieldnames = next(reader, None)
        if fieldnames is None:
            raise CsvDateContractError("CSV header is missing")
        missing = {"created_at", "sceneId"} - set(fieldnames)
        if missing:
            raise CsvDateContractError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
        created_at_index = fieldnames.index("created_at")
        scene_index = fieldnames.index("sceneId")

        for row_number, row in enumerate(reader, start=2):
            if len(row) == len(fieldnames):
                created_at = row[created_at_index].strip()
            elif delimiter == "\t" and len(row) > scene_index:
                created_at = row[-1].strip()
            else:
                raise CsvDateContractError(
                    f"row {row_number} has {len(row)} columns, expected {len(fieldnames)}"
                )
            match = DATE_PREFIX.match(created_at)
            if not match:
                raise CsvDateContractError(f"row {row_number} has an invalid created_at")
            try:
                row_date = date.fromisoformat(match.group(1))
            except ValueError as exc:
                raise CsvDateContractError(f"row {row_number} has an invalid created_at") from exc
            if row_date != expected_date:
                raise CsvDateContractError(
                    f"row {row_number} data date is {row_date.isoformat()}, expected {expected_date.isoformat()}"
                )

            row_scene = row[scene_index].strip()
            if row_scene != scene_id:
                raise CsvDateContractError(f"row {row_number} sceneId is {row_scene or '<empty>'}, expected {scene_id}")

            row_count += 1
            earliest_created_at = created_at if earliest_created_at is None else min(earliest_created_at, created_at)
            latest_created_at = created_at if latest_created_at is None else max(latest_created_at, created_at)

    if row_count == 0:
        raise CsvDateContractError("CSV contains no data rows")

    return {
        "status": "valid",
        "dataDate": expected_date.isoformat(),
        "sceneId": scene_id,
        "rowCount": row_count,
        "earliestCreatedAt": earliest_created_at,
        "latestCreatedAt": latest_created_at,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Teeni CSV data date before dashboard analysis.")
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--scene", default="488")
    args = parser.parse_args(argv)

    try:
        result = validate_csv_data_date(args.csv, args.date, args.scene)
    except (OSError, UnicodeError, csv.Error, CsvDateContractError) as exc:
        print(f"CSV date validation failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

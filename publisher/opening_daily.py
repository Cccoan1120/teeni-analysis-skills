"""Build a detached opening package after the daily base verifier succeeds."""
import argparse
import json
from pathlib import Path

from .session_structure import file_sha256


def load_package(directory):
    from .opening_cohorts import validate_opening_cohorts
    directory = Path(directory)
    def read(name):
        return json.loads((directory / name).read_text(encoding="utf-8-sig"))
    manifest = read("opening-manifest.json")
    if manifest.get("schemaVersion") not in ("teeni-opening-manifest/1.0.0", "teeni-opening-manifest/1.1.0", "teeni-opening-manifest/1.2.0", "teeni-opening-manifest/1.3.0", "teeni-opening-manifest/1.4.0"):
        raise ValueError("Invalid opening manifest")
    outputs = manifest.get("outputs", [])
    if len(outputs) != 2 or {entry.get("file") for entry in outputs} != {"opening-cohorts.json", "opening-verification.json"}:
        raise ValueError("Opening package is incomplete")
    for entry in outputs:
        if file_sha256(directory / entry["file"]) != entry.get("sha256"):
            raise ValueError("Opening package hash mismatch")
    value = read("opening-cohorts.json")
    verification = read("opening-verification.json")
    validate_opening_cohorts(value)
    if verification.get("schemaVersion") not in ("teeni-opening-verification/1.0.0", "teeni-opening-verification/1.1.0", "teeni-opening-verification/1.2.0", "teeni-opening-verification/1.3.0", "teeni-opening-verification/1.4.0") or verification.get("status") != "passed":
        raise ValueError("Opening package has not passed verification")
    for key in ("dataDate", "productVersion", "source", "rulesSha256"):
        if manifest.get(key) != value[key] or verification.get(key) != value[key]:
            raise ValueError("Opening package verification/source binding mismatch")
    for key in ("totalTurns", "totalSessions"):
        if verification.get(key) != value["totals"][key]:
            raise ValueError("Opening package verification totals mismatch")
    if manifest.get("crossDay") != value["crossDay"]:
        raise ValueError("Opening package cross-day binding mismatch")
    return value


def ready_from_manifest(manifest_path, primary_source=None):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    def entry(items, role):
        matches = [item for item in items if item.get("role") == role]
        if len(matches) != 1:
            raise ValueError(f"Expected one {role} entry")
        result = matches[0]
        if Path(result["file"]).name != result["file"]:
            raise ValueError("Unsafe manifest companion path")
        return result
    source = entry(manifest["sources"], "primary")
    detail = entry(manifest["outputs"], "primary_detail")
    workbook = entry(manifest["outputs"], "workbook")
    scene = str(manifest["sceneId"])
    if scene not in ("488", "904"):
        raise ValueError("Opening cohorts support M1 and M2")
    return dict(
        productVersion="M1" if scene == "488" else "M2",
        dataDate=manifest["dataDate"], sceneId=scene,
        detailPath=str(manifest_path.parent / detail["file"]), detailSha256=detail["sha256"],
        workbookPath=str(manifest_path.parent / workbook["file"]), workbookSha256=workbook["sha256"],
        sourcePath=str(Path(primary_source).resolve() if primary_source else manifest_path.parent / source["file"]),
        sourceSha256=source["sha256"], rowCount=source["rows"],
        manifestPath=str(manifest_path), manifestSha256=file_sha256(manifest_path), status="local_verified",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--primary-source", required=True, type=Path)
    parser.add_argument("--previous-manifest", type=Path)
    parser.add_argument("--previous-source", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.previous_source and not args.previous_manifest:
        parser.error("--previous-source requires --previous-manifest")
    ready = ready_from_manifest(args.manifest, args.primary_source)
    previous = ready_from_manifest(args.previous_manifest, args.previous_source) if args.previous_manifest else None
    from .opening_cohorts import write_package
    write_package(ready, previous, args.output_dir)


if __name__ == "__main__":
    main()

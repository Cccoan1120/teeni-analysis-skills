from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analysis import EARLIER_MANIFEST_SCHEMA, MANIFEST_SCHEMA, PREVIOUS_MANIFEST_SCHEMA, analyze
from .model import LocalCandidatePassthroughEnricher, QwenCandidateEnricher
from .verify import verify


DEFAULT_REGISTRY = Path(__file__).resolve().parent / "resources" / "entity-registry-v1.json"


def _history(paths: list[str]) -> list[dict]:
    return [json.loads(Path(path).read_text(encoding="utf-8-sig")) for path in paths]


def _add_analysis_arguments(parser: argparse.ArgumentParser, *, allow_model: bool) -> None:
    parser.add_argument("--detail", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--data-date", required=True)
    parser.add_argument("--history", action="append", default=[])
    parser.add_argument("--candidate-min-users", type=int, default=5)
    parser.add_argument("--candidate-min-sessions", type=int, default=8)
    if allow_model:
        parser.add_argument("--enable-candidate-model", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="teeni-interest-engine")
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze_parser = subparsers.add_parser("analyze")
    _add_analysis_arguments(analyze_parser, allow_model=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--output-dir", required=True)
    verify_parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    verify_parser.add_argument("--base-detail")
    backfill_parser = subparsers.add_parser("backfill-entity")
    _add_analysis_arguments(backfill_parser, allow_model=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            output = Path(args.output_dir)
            manifest_path = output / "teeni-interest-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            manifest_schema = manifest.get("schemaVersion")
            current_detail_contract = manifest_schema in {
                MANIFEST_SCHEMA,
                PREVIOUS_MANIFEST_SCHEMA,
                EARLIER_MANIFEST_SCHEMA,
            }
            current_private_contract = manifest_schema in {MANIFEST_SCHEMA, PREVIOUS_MANIFEST_SCHEMA}
            result = verify(
                output / "teeni-interest-report.xlsx",
                output / ("teeni-interest-detail.safe.csv" if current_detail_contract else "teeni-interest-detail.csv"),
                output / "teeni-interest-snapshot.json",
                output / "teeni-interest-candidates.private.json",
                manifest_path,
                args.registry,
                output / "teeni-interest-detail.csv" if current_private_contract else None,
                args.base_detail,
                output / "teeni-interest-segments.json" if manifest_schema == MANIFEST_SCHEMA else None,
            )
            payload = {
                "ok": result.ok,
                "detailRows": result.detail_rows,
                "privateDetailRows": result.private_detail_rows,
                "outputs": result.output_count,
                "sheets": result.sheet_count,
            }
        else:
            if args.command == "backfill-entity":
                enricher = LocalCandidatePassthroughEnricher()
            else:
                enricher = QwenCandidateEnricher.from_environment() if args.enable_candidate_model else None
            artifacts = analyze(
                args.detail,
                args.manifest,
                args.registry,
                args.output_dir,
                data_date=args.data_date,
                history=_history(args.history),
                enricher=enricher,
                candidate_min_users=args.candidate_min_users,
                candidate_min_sessions=args.candidate_min_sessions,
            )
            payload = {
                "ok": True,
                "productVersion": artifacts.snapshot["productVersion"],
                "sceneId": artifacts.snapshot["sceneId"],
                "manifestPath": str(artifacts.manifest_path),
                "snapshotPath": str(artifacts.snapshot_path),
                "segmentPath": str(artifacts.segment_path),
                "detailPath": str(artifacts.private_detail_path),
                "safeDetailPath": str(artifacts.detail_path),
                "candidateDegraded": artifacts.snapshot["method"]["candidateDegraded"],
            }
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

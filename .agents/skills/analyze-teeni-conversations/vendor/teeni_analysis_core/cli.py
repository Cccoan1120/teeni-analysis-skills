from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .reporting import build_report_model


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="teeni-analysis")
    subcommands = parser.add_subparsers(dest="command", required=True)
    report = subcommands.add_parser("report-model")
    report.add_argument("--primary", required=True)
    report.add_argument("--primary-scene", required=True)
    report.add_argument("--baseline")
    report.add_argument("--baseline-scene")
    report.add_argument("--primary-detail", required=True)
    report.add_argument("--primary-ending-detail", required=True)
    report.add_argument("--baseline-detail")
    report.add_argument("--primary-label", default="新版")
    report.add_argument("--baseline-label", default="基准版")
    report.add_argument("--rules")
    report.add_argument("--overrides")
    report.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = Path(args.output).resolve()
        inputs = [Path(args.primary).resolve()]
        if args.baseline:
            inputs.append(Path(args.baseline).resolve())
        if output in inputs or (
            output.exists() and any(output.samefile(source) for source in inputs)
        ):
            raise ValueError("output must not resolve to a source CSV")
        report = build_report_model(
            primary=args.primary,
            primary_scene=args.primary_scene,
            primary_detail=args.primary_detail,
            primary_ending_detail=args.primary_ending_detail,
            baseline=args.baseline,
            baseline_scene=args.baseline_scene,
            baseline_detail=args.baseline_detail,
            primary_label=args.primary_label,
            baseline_label=args.baseline_label,
            rules_path=args.rules,
            overrides=args.overrides,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps({"output": str(output), "schemaVersion": report["schemaVersion"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

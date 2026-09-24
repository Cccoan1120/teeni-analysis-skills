"""Run verified local interest batches without a Django database."""

import argparse
import json
import sys
from pathlib import Path

from .batch import analyze_batch, load_batch_manifest, load_batch_result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyze or verify a Teeni interest batch")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--manifest", required=True)
    analyze.add_argument("--output-dir", required=True)
    analyze.add_argument("--cache-dir")
    verify = commands.add_parser("verify")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--result-manifest", required=True)
    args = parser.parse_args(argv)

    try:
        contract = load_batch_manifest(args.manifest)
        if args.command == "analyze":
            output = Path(args.output_dir).resolve()
            if output.exists():
                raise ValueError("output directory already exists")
            artifacts = analyze_batch(contract, output, cache_dir=args.cache_dir)
        else:
            artifacts = load_batch_result(args.result_manifest, contract)
        print(json.dumps({
            "ok": True,
            "productVersion": contract.product_version,
            "dates": [entry.data_date.isoformat() for entry in contract.entries],
            "resultManifest": str(artifacts.manifest_path),
            "verified": True,
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

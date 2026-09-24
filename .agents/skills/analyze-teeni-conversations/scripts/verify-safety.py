"""Rebuild safety results from immutable input before auditing the review workbook."""
import csv
import hashlib
import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent / 'vendor'))
from teeni_analysis_core.reporting import build_report_model


def main():
    request = json.load(sys.stdin)
    manifest_path = Path(request['manifest'])
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    entries = {item['role']: item for item in manifest['outputs']}
    if len(entries) != len(manifest['outputs']):
        raise ValueError('duplicate output roles')
    paths = {}
    for role in ('effective_rules', 'safety_workbook', 'safety_exclusions'):
        item = entries[role]
        if Path(item['file']).name != item['file'] or any(char in item['file'] for char in '\\/:'):
            raise ValueError('unsafe companion filename')
        path = manifest_path.parent / item['file']
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError(f'{role} hash mismatch')
        paths[role] = path
    primary = next(row for row in manifest['sources'] if row['role'] == 'primary')
    source = Path(request.get('source') or manifest_path.parent / primary['file'])
    if hashlib.sha256(source.read_bytes()).hexdigest() != primary['sha256']:
        raise ValueError('source hash mismatch')
    detail = Path(request['detail'])
    with tempfile.TemporaryDirectory(prefix='teeni-safety-audit-') as folder:
        temp = Path(folder)
        regenerated = temp / 'detail.csv'
        report = build_report_model(primary=source, primary_scene=manifest['sceneId'], primary_detail=regenerated,
                                    primary_ending_detail=temp / 'ending.csv', rules_path=paths['effective_rules'])
        if regenerated.read_bytes() != detail.read_bytes():
            raise ValueError('independently rebuilt base detail differs from package')
        report['coreVersion'] = manifest['coreVersion']
        report_path = temp / 'report.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        result = subprocess.run([sys.executable, str(SCRIPTS / 'safety-review.py'), 'verify',
                                 '--report', str(report_path), '--detail', str(detail),
                                 '--data-date', manifest['dataDate'], '--output', str(paths['safety_workbook']), '--exclusions', str(paths['safety_exclusions'])],
                                capture_output=True, text=True, encoding='utf-8')
        if result.returncode:
            raise ValueError(result.stdout + result.stderr)
        if entries['safety_exclusions'].get('rows') != len(report['primary']['safetyGameExclusions']):
            raise ValueError('excluded hit count mismatch')
        with detail.open(encoding='utf-8-sig', newline='') as handle:
            by_record = {row['id']: row['source_row'] for row in csv.DictReader(handle)}
        user_issues = defaultdict(list)
        for item in report['primary']['userSafetyIssues']:
            user_issues[by_record[item['record_id']]].append({key: item[key] for key in ('category', 'severity', 'evidence')})
        print(json.dumps({'userIssues': list(user_issues.items()), 'excludedHits': len(report['primary']['safetyGameExclusions'])}))


if __name__ == '__main__':
    main()

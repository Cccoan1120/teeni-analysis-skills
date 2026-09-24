import hashlib
import hmac
import json
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from interest_engine.batch import load_batch_manifest, load_batch_result
from interest_engine.contracts import sha256_file
from interests.preferences import _secret, _identity, build_contribution


class Command(BaseCommand):
    help = "Prepare a signed aggregate publication from reviewed, verified local artifacts."

    def add_arguments(self, parser):
        parser.add_argument('--manifest', required=True)
        parser.add_argument('--result-manifest', required=True)
        parser.add_argument('--output-dir', required=True)

    def handle(self, *args, **options):
        contract = load_batch_manifest(options['manifest'])
        if not contract.candidate_review_complete:
            raise CommandError('candidate review must be completed')
        artifacts = load_batch_result(options['result_manifest'], contract)
        root = Path(options['output_dir']).resolve()
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
        for entry, artifact in zip(contract.entries, artifacts.daily):
            destination = root / entry.data_date.isoformat()
            destination.mkdir(mode=0o700)
            contribution = build_contribution(entry.detail_path, artifact.detail_path, contract.registry_path, artifact.snapshot)
            private = destination / 'contribution.private.json'
            private.write_text(json.dumps(contribution, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
            files = {'contribution': private}
            for role, source, name in (('snapshot', artifact.snapshot_path, 'snapshot.json'),
                                       ('segments', artifact.segment_path, 'segments.json'),
                                       ('workbook', artifact.workbook_path, 'interest-report.xlsx')):
                files[role] = destination / name
                shutil.copy2(source, files[role])
            payload = {'schemaVersion': 'teeni-interest-aggregate-publication/1.0.0',
                       'productVersion': contract.product_version, 'dataDate': entry.data_date.isoformat(),
                       'identitySha256': _identity(), 'candidateReviewComplete': True,
                       'artifacts': {role: {'file': path.name, 'sha256': sha256_file(path)} for role, path in files.items()}}
            payload['signature'] = hmac.new(_secret(), json.dumps(payload, sort_keys=True, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
            (destination / 'package.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        self.stdout.write(json.dumps({'status': 'prepared', 'productVersion': contract.product_version, 'days': len(artifacts.daily)}))

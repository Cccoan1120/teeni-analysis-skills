---
name: analyze-teeni-interests
description: Analyze Teeni M1 (scene 488) and M2 (scene 904) IP and entity interests from verified base-detail packages, then prepare and publish verified aggregate results to the matching dashboard. Use for IP rankings, interest sessions and queries, gender, age, and region preferences. Raw-export base analysis is handled separately.
---

# Teeni Interest Analysis

Use the `interest_engine/` package and Django management commands in this repository. Resolve paths from the cloned repository; never assume another person's machine layout. Keep M1 and M2 inputs, outputs, caches and publication targets separate.

## Inputs and Classification

- Accept verified `teeni-base-detail/1.2.0` packages and their base manifests. M1 is scene 488; M2 is scene 904. Raw exports first need `$analyze-teeni-conversations`.
- Derive the product from the manifest and CSV scene fields, not filenames. Each batch contains one product and 1-7 consecutive dates; daily work uses one date.
- Use the frozen `db-v3` registry in `interest_engine/resources/entity-registry.db-v3.json` (SHA-256 `6c1f0d701146f73cb16d222ff60c45d003896e940debd05e2085fd57a069d3f2`), or an explicitly reviewed replacement. The batch manifest must point to that exact file or an identical private copy and bind it and every input with SHA-256. Candidate discovery does not approve new entities.
- Classify locally by default. Do not invoke the optional candidate model or send conversation text outside the analyst machine without explicit authorization.
- Keep `teeni-interest-detail.csv` and `teeni-interest-detail.safe.csv` private. The first contains original query/reply and readable `entity_names` without `entity_ids`; the second contains pseudonymous IDs and classification evidence. Neither is a public dashboard download.
- Preserve the engine's per-entity signals, preference direction, unknown-behavior, context-inheritance and demographic contracts. Attention, active interest and satisfaction are different claims.

## Analyze And Verify

Create a UTF-8 `teeni-interest-batch/1.0.0` manifest outside Git. Use paths relative to that manifest or absolute paths on the analyst machine:

```json
{
  "schemaVersion": "teeni-interest-batch/1.0.0",
  "registryPath": "entity-registry.json",
  "registrySha256": "<sha256>",
  "candidateReviewComplete": false,
  "packages": [{
    "dataDate": "YYYY-MM-DD",
    "detailPath": "teeni-base-detail.csv",
    "manifestPath": "teeni-base-manifest.json",
    "detailSha256": "<sha256>",
    "manifestSha256": "<sha256>"
  }]
}
```

From the repository root:

```text
python -m interest_engine.batch_cli analyze --manifest <batch.json> --output-dir <new-private-output-dir> --cache-dir <private-cache-dir>
python -m interest_engine.batch_cli verify --manifest <batch.json> --result-manifest <output/teeni-interest-batch-result.json>
```

The engine validates source hashes, dates, scenes, workbook and private detail. Retain the result manifest and all referenced artifacts. A retry with the same verified result should use `verify`, not reclassify. Keep caches private with a local retention policy.

## Publish Verified Aggregates

Review candidate decisions and the frozen registry first. Set `candidateReviewComplete=true` only after an actual review, then analyze and verify using that final manifest. The registry hash and result must match. Publish the corresponding verified base aggregate before the interest aggregate.

Use the repository's Django commands locally to build a signed package. Set `TEENI_INTEREST_IDENTITY_KEY` through the analyst's protected environment to the same authorized value used by the destination dashboard; never place it in Git, command arguments or logs.

```text
python manage.py prepare_interest_aggregates --manifest <reviewed-batch.json> --result-manifest <verified-result.json> --output-dir <new-private-bundle-dir>
```

Transfer only each date's signed bundle to the authorized dashboard host. Its `contribution.private.json` is private pseudonymous data and must never become a public download. On the compatible server, load its protected environment and run:

```text
python manage.py import_interest_aggregates --package <bundle/date/package.json>
python manage.py import_interest_aggregates --package <bundle/date/package.json> --apply --backup-dir <new-private-backup-dir>
```

The first command validates without writing. The second requires a new scoped backup directory and replaces only the matching product/date. If a response is ambiguous, inspect the date, source hash and receipt on the destination before retrying the same package. A local import is not evidence that the website changed; confirm target snapshot, cumulative coverage and health before reporting online success. The dashboard app on the target must support the same aggregate publication contract.

## Cumulative Preferences

M1 and M2 can coexist on the same date and do not share rankings or contributions. Day, calendar-month and all-time rankings use distinct users within one compatible product definition; daily user counts must not be added. The target identity key must stay stable across days. Interest definitions, registry hashes and base contracts must match for comparison; excluded incompatible dates are not missing data.

## Human Semantic Evaluation

Verification proves artifact integrity and arithmetic, not semantic accuracy. Prepare private stratified review samples from the analyst detail:

```text
python -m interest_engine.evaluation prepare --detail <teeni-interest-detail.csv> --output <new-private-review-dir> --per-stratum 30
python -m interest_engine.evaluation evaluate --labels <semantic-review.private.csv> --predictions <semantic-predictions.private.json> --output <semantic-evaluation.json>
```

Keep expected labels blank until human reviewers complete them. Record two distinct annotators and any adjudicator truthfully. Report reviewed counts and missing strata; zero reviewed samples means accuracy is null. Pending human review does not block deterministic analysis, but does block a claim of human-validated classification quality.

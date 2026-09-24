---
name: analyze-teeni-conversations
description: Analyze Teeni raw conversation CSV exports entirely locally. Use when Codex needs opening conversion, raw/non-template/net-valid depth, five-plus-turn ending signals, 对/嗯 fixed-fallback reopen behavior, strict scene 488, 901, or 904 intents, age/gender, city, and constellation analysis, AI response quality and safety review, independent user query risk-expression review with P0/P1/P2 user-level triage, session summaries, or optional baseline comparison, and must produce a verified Excel workbook, detail CSVs, and hash manifest without topic classification or external model calls.
---

# Analyze Teeni Conversations

Create a local, auditable base analysis package. Keep opening conversion, engaged depth, AI quality, AI safety, user query risk review, and ending signals separate; never collapse them into one score.

## Validate Inputs

- Require one primary raw CSV and an explicit `sceneId`. Accept a baseline only with its own explicit `sceneId`.
- Require `id`, `clientId`, `cid`, `text`, `response`, `timestamp`, `intention`, `subIntention`, `created_at`, `sceneId`, and `model`.
- Confirm every row matches its supplied scene and the file contains data rows.
- Treat `cid` as the conversation key and `clientId` as the user key.
- Keep source CSVs in place. Never copy real exports into the Skill or Git.

## Prepare The Runtime

Use the Python environment and `TEENI_NODE_MODULES` described in the repository README. When Codex provides `load_workspace_dependencies`, use it to locate the bundled Node packages. Keep inputs and outputs outside Git. The linked local analysis and publication command is `scripts/analyze-and-publish-dashboard.ps1` at the repository root.

Do not require or read `DASHSCOPE_API_KEY` or `DASHSCOPE_BASE_URL`. Do not run semantic preflight or call another Skill implicitly.

## Run The Base Analysis

```text
node scripts/analyze-shared.mjs
  --primary <raw.csv>
  --primary-scene <sceneId>
  --data-date <YYYY-MM-DD>
  [--baseline <raw.csv> --baseline-scene <sceneId>]
  [--primary-label <name>]
  [--baseline-label <name>]
  [--output-dir <directory>]
  [--overrides <inline-json-or-json-file>]
```

Use core `2.7.0`, rules `16.0.0`, report model `teeni-base-report-model/2.6.0`, and detail contract `teeni-base-detail/1.2.0` (additive `model` column). Pass the user-confirmed business date explicitly. Never overwrite existing outputs; keep one shared version suffix for companion files.

## Apply Base Contracts

- Management depth uses `teeni-session-structure/1.0.0`: raw rows / distinct cid; sessions with at least 2 raw rows / all sessions; their raw rows / those sessions; sessions with at least 5 raw rows / all sessions. Each daily file describes only its visible records. Do not subtract first turns, classify text, infer engagement, or combine cross-day sessions. A zero denominator yields null.
- The exact user text `##{startPrompt}##` is a confirmed automatic opening under rules 16.0.0. Exclude it consistently from reviewable requests, scoreable replies and net-valid specialty metrics. Do not broadly exclude other placeholder-like text or change confirmed `对`/`嗯` rules. Structural counts always retain the raw row.
- Reconstruct each `cid` by `timestamp`, `created_at`, then `source_row`.
- Parse only `response.generated_text` into `ai_text`. Preserve parse failures and empty replies as quality states; never put full `response` JSON in detail outputs.
- For `488` and aliases `901`/`904`, read real intent only from `response.extra.intent_name`, split once on `｜`, and never fall back to CSV intent columns.
- For demographic-enabled scenes, read age, gender, birthday, and city only from `response.extra`, resolve them by `clientId`, and keep missing, invalid, mixed legal/illegal, conflict, and stable states separate. Accept birthdays only as legal `YYYY-MM-DD` dates and map normal values to the fixed Western twelve-sign boundaries. Normalize cities with NFKC, leading/trailing whitespace cleanup, and removal of one trailing `市`; preserve `地区`, `自治州`, `盟`, and `特别行政区` without province mapping.
- Remove only sessions with no non-template user input for `剔除单轮开场白后分析`.
- Compute net-valid turns separately by excluding configured invalid user or AI template content. Empty or failed AI replies remain quality issues.
- Keep two five-plus ending cohorts. For the valid cohort, require at least five net-valid turns and select the final net-valid row. For the unfiltered cohort, require at least five raw turns and select the raw final row, even when it is invalid.
- In the unfiltered cohort, measure configured `对`/`嗯` fixed fallbacks and join the earliest later different `cid` for the same `clientId`. Keep `(0, 300]` as the five-minute KPI. Also report an exclusive session-level delay distribution through 24 hours, split by `对` and `嗯`, with shares of all triggers and observed next sessions. For bounded bands, provide the count observable through the upper bound. Leave the open `24小时以上` denominator blank; treat no-next rows as right-censored, not permanent churn. Retain join evidence in the unfiltered detail sheet. Describe this as a behavioral proxy for chat continuation intent, never as proof of true motive or strong chat desire.
- Select each final row by `timestamp`, `created_at`, then `source_row`. Assign exactly one observable ending category and keep follow-up, boundary, quality, and safety flags separate. Record final-turn invalid state and reason for the unfiltered cohort. Do not infer a true reason for leaving.
- Continue to use the valid cohort for sub-intent comparison and demographic behavior. Age/gender, city, and constellation behavior denominators include only sessions with at least one net-valid turn; profile coverage and composition retain all users.
- Review AI safety only over scoreable `AI response` text, excluding fixed-opening rows from both candidate counts and denominators. Exclude a risk hit only when a direct prevention or warning phrase applies to that hit; an unrelated clause must not suppress it. Privacy-inducement phrases such as `不要告诉妈妈` remain candidates. All AI safety statuses are `候选`, including self-harm mentions; rules never confirm an issue.
- Independently review every non-empty user query except fixed openings, injected topic openings, and confirmed invalid user templates. Do not skip user review because the AI response is empty, failed to parse, or used a fallback. Do not apply AI de-escalation exclusion to user queries.
- Emit one user risk-expression candidate per matched category, but count unique matched queries for the user risk-expression rate. Aggregate by `clientId`: P0 for self-harm/child-inappropriate, sexual, or child-privacy categories; otherwise P1 for at least three matched queries, two sessions, or two categories; P2 for the remainder. Treat priority only as review order.
- Call all safety outputs candidates. Never equate a rule hit with a confirmed dangerous user.

## Produce The Base Package

Write beside the primary CSV unless directed otherwise:

- one lightweight base `.xlsx` report;
- one UTF-8 BOM base per-turn detail CSV for each analyzed version;
- one UTF-8 BOM primary five-plus-turn ending-detail CSV;
- one non-sensitive base manifest JSON.
- one effective-rules JSON companion, hashed as the manifest's `effective_rules` output. Verification uses this archived merged configuration, including one-run overrides.
- one aggregate-only `teeni-base-supplemental-metrics/1.0.0` JSON companion, hashed with output role `supplemental_metrics`. It includes explicit count/denominator/null-rate contracts for unique quality candidates, missing reply text, opening funnel, early experience, continuation proxies, and mature five-minute reopen proxies. Preserve source `model` in detail and aggregate model counts.
- one aggregate-only session structure JSON companion hashed as `session_structure`. The wrapper binds dataDate, sceneId, coreVersion and rulesVersion and holds primary/optional baseline metrics. Each metric object includes schemaVersion, totalTurns, totalSessions, singleTurnSessions, multiTurnSessions, multiTurnTurns, fivePlusSessions, averageTurns, multiTurnRate, multiTurnAverageTurns and fivePlusRate. The manifest hashes bind it to source and detail; the independent verifier recounts detail identities after source reconciliation. Display the four indicators with their numerators and denominators in the existing overview sheet.

Reject missing identity keys, duplicate record IDs, cross-user session keys, nonfinite/nonpositive timestamps, and millisecond/mixed timestamp units before analysis; source repair must be explicit and preserve originals. Missing models and non-epoch timestamps remain visible audit counts. Never claim export completeness from the maximum record timestamp.

Keep historical raw opening continuation and observed reopen ratios labeled as such. Main new funnel deduplicates consecutive fixed-opening retries, then measures real user reply, first effective response, and at least 3/5 effective user/AI turns against the same exposure population. Eligible user requests survive AI failure. Mature reopen includes only triggers observed for the full 300 seconds under the maximum-record-time proxy; no mature sample means null. Daily files cannot establish cross-day completeness or retention. Early stopping and continuation are proxies, not satisfaction, recovery, or confirmed churn. Empty generated text is not a confirmed device-action failure.

The base detail must include `timestamp`, ordering fields, `text`, `ai_text`, validity, real intent, existing AI quality/safety signals, `user_query_reviewable`, `user_safety_signals`, `user_safety_issue_count`, `user_review_priority`, local raw `birthday`/`city`, and resolved demographic fields. It must exclude all topic/semantic columns and full `response`.

The manifest must use `teeni-base-bundle-manifest/1.0.0`, store scene/core/rules/contract versions, relative filenames, row counts, and SHA-256 values for source and output files. Never store absolute paths, credentials, endpoint data, or conversation text in the manifest.

Put full five-plus ending details in `五轮以上结束明细` and `五轮以上结束明细（不剔除无效）` inside the workbook. Make both sheets filterable and freeze their header rows. Keep the existing valid-cohort ending-detail CSV for compatibility; do not add an unfiltered ending CSV.

Keep `新版安全问题` scoped to AI responses. Add complete filterable `需关注用户汇总` and `用户风险表达明细` sheets after it, freeze their header rows, and never cap their candidates. Show AI safety, user risk-expression, candidate-user, and P0/P1/P2 metrics separately in `结论总览`; distinguish `AI质量`, `AI安全`, and `用户query风险` in `新版问题总览`.

Use exactly 20 sheets for ordinary primary mode and 23 for demographic-enabled `488`/`901`/`904`. Insert `版本基准对比` after `结论总览` in comparison mode. Use `五轮以上结束分析（不剔除无效）`, `真实意图分析`, `年龄性别概览`, `所在地分析`, and `星座分析`; do not create any topic or initiative sheet.

## Verify Before Delivery

Run `scripts/verify.mjs` with the workbook, primary detail, ending detail, manifest, scene, and optional baseline detail. Require:

Supply `--primary-source <raw.csv>` and, in comparison mode, `--baseline-source <raw.csv>` when sources are not beside the manifest. Verification needs Python for independent source/detail reconciliation and XLSX error-cell inspection. Do not move source CSVs just to satisfy lookup.

The verifier also accepts archived core/rules 2.5.1/13.0.0, 2.6.0/14.0.0 and 2.6.1/15.0.0 bundles using their archived effective rules. Never invent missing companions for archived packages. New outputs use 2.7.0/16.0.0 and require the structural companion, safety workbook and exclusion evidence. This preserves verified-package retry compatibility; old safety metrics retain their historical interpretation.

## Daily Safety Review

Every analysis also creates a private `Teeni安全复核_YYYYMMDD_M1|M2.xlsx` beside the source, with overview, AI response candidates, user risk expressions, attention-user summary, complete session context, and game exclusions. Keep review conclusions blank and status pending. All candidates are included; distinguish category events from unique records and keep AI/user populations separate. Original text and account identifiers stay local. Include `safety_workbook` and `safety_exclusions` in the hash manifest; failed generation or verification blocks publication.

Rules 15.0.0 exclude only exact bomb-token matches in established number-bomb guessing games. Context is processed user-before-AI in session order, expires on an unclear or changed topic, and never crosses accounts/sessions. A later reply cannot exempt an earlier user query. Real bomb activity, threats, injury instructions and other matched categories remain candidates. Save every game exclusion with its source match offsets, context record and reason for local audit. Do not whitelist entire game conversations or other games without reviewed evidence and regression cases.

Safety revisions do not authorize changing interest classifications or previous annotations. When replacing a published base package, reconcile interest-consumed input fields and classifications and regenerate the affected date's verified interest package. Safety history uses exact contracts.

- exact sheet order and count;
- manifest hashes and relative filenames;
- source/detail row reconciliation and contiguous unique `source_row`;
- no `response`, topic, or semantic columns in base detail;
- recomputed user-query eligibility, rule matches, unique-query denominator, user aggregation, P0/P1/P2 assignment, and priority sorting;
- complete filterable and frozen user summary/detail sheets with exact row counts;
- strict intent parsing, birthday/city normalization, twelve-sign boundaries, and demographic conflict states;
- valid ending `cid` uniqueness and joins to the final net-valid row, including AI/user/compatibility-union safety fields;
- unfiltered workbook detail count and joins to each raw final row, including invalid final-turn audit fields;
- fixed-fallback trigger, same-user next-session join, `(0, 300]` five-minute KPI, exclusive delay-band boundaries, dual denominators, censoring limits, and session/user aggregates;
- zero formula errors;
- every sheet rendered and nonblank.

State that analysis ran locally. IP/entity interest analysis uses `$analyze-teeni-interests` with a verified base package. The old topic workflow is retired. Demand satisfaction requires separately labeled evaluation evidence; never infer it from turn count or silently call external models.

## Publish Verified Base Results

For a daily scene 488 or 904 CSV, use the repository's `scripts/analyze-and-publish-dashboard.ps1` with explicit `-Csv`, `-DataDate`, and `-ProductVersion`. It validates every row's date and scene, generates and independently verifies the base package, then sends only the aggregate snapshot to `TEENI_DASHBOARD_PUBLISH_URL` using `TEENI_PUBLISH_TOKEN` from the local environment. The command also prepares the verified opening-cohort aggregate. Never publish a pre-existing package through this linked command. Raw CSVs and per-turn outputs remain local. Require the matching receipt and dashboard health before reporting an online update.

For a deliberate retry of an already verified package, use `scripts/publish-dashboard.ps1` with explicit workbook, manifest, source CSV and date. Check the target state first if the previous response was ambiguous. Keep real CSVs, reports, previews, manifests, caches, absolute paths and identifiers out of Git. Treat `--overrides` as one-run only; behavior changes to defaults require a rules-version update and focused tests.

## Resources

- Read `references/methodology.md` for metric definitions and limitations.
- Use `references/default-rules.json` for deterministic local rules.
- Use `references/regression-cases.json` only as anonymous regression material.
- Use `scripts/analyze-shared.mjs` to run and `scripts/verify.mjs` to verify.

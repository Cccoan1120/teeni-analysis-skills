# Teeni Base Analysis Methodology

## Contents

1. Input and ordering
2. Cohorts and depth
3. Intent and demographics
4. Ending, quality, and safety
5. Output contract

## Input And Ordering

Require explicit `sceneId` and validate every row. Use `cid` as the session key and order rows by numeric `timestamp`, `created_at`, then `source_row`. Keep source rows immutable.

Parse `response.generated_text` into `ai_text`. Keep JSON failures and empty generated text as separate quality states. Never copy full `response` into derived CSVs.

Reject missing `id`/`clientId`/`cid`, duplicate `id`, a `cid` spanning users, nonfinite/nonpositive timestamps, and millisecond or mixed-unit timestamps. Preserve `model` in local detail and aggregate its counts. Missing models and non-epoch timestamps are audit counts. The source-local `created_at` timezone is unspecified; timestamp seconds drive durations. These checks do not prove complete export coverage.

## Cohorts And Depth

Management indicators use a separate `teeni-session-structure/1.0.0` contract. Count every raw row per `cid` within the daily file: average turns = total rows / sessions; multi-turn rate = sessions with >=2 rows / sessions; multi-turn average = their rows / their sessions; five-plus rate = sessions with >=5 rows / sessions. Zero denominator yields null. These measures ignore all templates and validity classifications and do not prove real participation or satisfaction. A daily slice cannot establish full cross-day session length.

Rules 16.0.0 treat only the confirmed exact literal `##{startPrompt}##` as an automatic opening in content-based specialty metrics. Preserve it in structural row counts. Explanatory text mentioning that literal remains an ordinary request. User risk-expression and attention-user rates use null for an empty eligible population.

- Raw depth: all rows in a session.
- Non-template depth: exclude fixed opening templates; remove a session only when no non-template user input remains.
- Net-valid depth: exclude each configured invalid user or AI template row; keep remaining rows in mixed sessions.

Use net-valid depth for valid five-turn qualification, valid final-turn selection, intent comparison, and demographic behavior. Separately retain an unfiltered ending cohort based on raw depth. Do not silently treat empty AI replies as valid quality outcomes.

## Intent And Demographics

For scenes `488`, `901`, and `904`, parse intent only from `response.extra.intent_name`. Require exactly one full-width `｜` and non-empty parts. Do not fall back to `intention` or `subIntention`.

For demographic-enabled scenes, read age, gender, birthday, and city only from `response.extra`. Resolve values by `clientId`:

- stable one-value profiles are normal;
- missing, illegal, mixed legal/illegal, and conflicting values remain distinct;
- ages 1-17 use bands `1-2`, `3-4`, `5-6`, `7-9`, and `10-17`;
- `18+` is audit-only;
- groups below 30 independent users support audit, not preference claims.

Accept birthdays only when they are legal Gregorian dates in exact `YYYY-MM-DD` form. Map normal birthdays to the common Western twelve-sign boundaries. Apply NFKC and leading/trailing whitespace cleanup to cities, then remove one trailing `市`; preserve labels ending in `地区`, `自治州`, `盟`, or `特别行政区` and do not infer a province. City and constellation composition shares use normal users for that field as the denominator; field coverage uses all independent users.

## Ending, Quality, And Safety

Maintain two ending views using the same deterministic category rules:

- Valid view: require at least five net-valid turns and select the final net-valid row.
- Unfiltered view: require at least five raw turns and select the raw final row, including an invalid template row when it is last.

For the unfiltered view, retain final-turn invalid state and reason. In both views, keep confidence, evidence, AI follow-up, export-boundary risk, quality, and safety flags separate. Never infer the user's true reason for leaving.

Within the unfiltered view, separately measure the configured `对`/`嗯` fixed-fallback sequence. Require the raw final query and AI reply to exactly match their normalized configured values, then join to the earliest different `cid` for the same `clientId` whose first timestamp is strictly later. Count `(0, 300]` seconds as a five-minute reopen and retain its session-level, unique-user-level, and trigger-word KPIs.

Report the complete next-session delay distribution at session level only. Use consecutive left-open, right-closed bands through 24 hours, followed by an open `>24 hours` band and a separate no-next row. For every timed band, divide once by all trigger sessions and once by trigger sessions with an observed next `cid`. For each bounded band, define the observable sample as a trigger whose next session occurred on or before the upper bound, or whose export-cutoff observation time reached that upper bound. Do not provide an observable denominator for the open `>24 hours` band. Leave the observed-next share blank for no-next rows and disclose that they include right-censored samples, so they cannot be interpreted as permanent churn. Retain row-level join evidence. Treat all of these measures only as behavioral proxies for chat continuation intent, not proof of a user's true motive or strong desire to chat.

AI quality and AI safety outputs remain automated candidates over AI responses. AI safety considers only non-empty replies outside fixed-opening rows, in both numerator and denominator. The numerator counts matched categories, so it is a candidate count per scoreable reply and can exceed 100% when one reply matches multiple categories. Every AI safety status is `候选`; a self-harm keyword cannot establish a confirmed issue. Exclude a matched risk phrase only when a direct prohibition immediately precedes it or a direct warning immediately follows it in the same clause. Unrelated mentions of a doctor, help, or warnings elsewhere do not suppress a hit. Child-privacy inducements such as `不要告诉妈妈` and `这是我们的小秘密` are never excluded by this warning rule. De-escalation exclusion remains AI-only.

From core 2.6.0 / rules 14.0.0, `qualityRate` means unique scoreable replies with at least one quality candidate / scoreable replies, with null for zero denominator. All candidate events remain available separately. `qualityCandidateEventsPer100Responses` uses only events on the same scoreable replies and can exceed 100. Missing reply text and parse failure use all reviewable user requests as the denominator, retaining requests when AI fails. Empty generated text alone cannot distinguish failed generation from structured device actions. Broad negation inside ordinary statements is no longer a correction candidate; explicit response-directed correction and stand-alone negation remain candidates, not confirmed misresponses.

New opening funnel: consecutive fixed-opening rows form one exposure block. Count real user replies only from reviewable queries before the next opening block. The first response is effective only when that first real request has nonempty AI text and passes existing invalid-content rules. Subsequent 3/5 effective-turn stages require that first effective response and at least 3/5 effective request/reply pairs within the same block. All stages divide by the same deduplicated exposure population. Raw historical opening follow-on rows remain unchanged and must not be described as real replies.

The supplemental mature five-minute reopen rate includes only triggers at least 300 seconds before the maximum record timestamp; count successes only in that mature population. Show immature count and null if none are mature. The maximum record timestamp is an observation proxy, not an authenticated export watermark. Historical all-trigger observed ratios remain for compatibility. No-next, early stop after real request 1-4, and fallback/correction continuation are within-file observations; they cannot establish churn, recovery, satisfaction, or cross-day completeness. Do not infer D1/D7 retention from this single-day artifact.

For age/gender, city, and constellation behavior, retain only sessions with at least one net-valid turn in session counts, average-turn denominators, and five-plus-turn denominators. Coverage and user-composition denominators continue to include all observed users, including those with only invalid sessions.

Audit user queries independently. A query is reviewable when it is non-empty and is not a fixed opening, injected topic opening, or confirmed invalid user template. AI parse failure, empty response, fallback response, or de-escalating response never removes an otherwise reviewable query. Match the configured seven user-side categories without the AI de-escalation exclusion. Emit one candidate row per matched category, while defining the user risk-expression numerator as unique matched queries and the denominator as all reviewable user queries.

Aggregate matched queries by `clientId` for review ordering only:

- P0: any self-harm/child-inappropriate, sexual, or child-privacy category;
- P1: otherwise at least three matched queries, two matched sessions, or two categories;
- P2: all remaining candidates.

Within a priority, sort by matched sessions, matched queries, categories, then latest hit time, all descending. Define the candidate-user rate as users with at least one matched query divided by users with at least one reviewable query. Never describe a rule hit as a confirmed dangerous user.

For ending details, keep `末轮AI安全信号` and `末轮用户风险信号` separate. Preserve `末轮安全信号` as a compatibility column containing the ordered union of actual candidates from both sides.

## Output Contract

The per-category quality issue overview retains all candidate events, including opening rows and missing replies, and divides each category by all raw records; this audit frequency is distinct from the unique scoreable-reply KPI. Display that denominator explicitly. Fallback continuation events include non-opening requests whose reply has the configured ASR-repeat candidate, even when the query was invalid under the net-valid depth rules; count a later reviewable query in the same observed session as continuation.

Use `teeni-base-report-model/2.6.0` for the workbook model, `teeni-base-detail/1.2.0` for base per-turn detail, and `teeni-base-bundle-manifest/1.0.0` for the adjacent manifest. The detail contains ordering, user/AI text, validity, intent, AI quality/safety, user-query reviewability/risk signals/priority, local raw birthday/city, and resolved profiles, but no full response or topic fields. The workbook contains both full ending-detail views plus uncapped `需关注用户汇总` and `用户风险表达明细` filterable sheets and aggregate-only location/constellation analysis. The existing valid ending CSV remains the only companion ending CSV. The manifest contains only relative filenames, versions, hashes, row counts, scene, and run metadata; never paths, credentials, or conversation text.

The hashed `supplemental_metrics` companion uses schema `teeni-base-supplemental-metrics/1.0.0`, with `primary` and optional `baseline`. Each holds `quality`, `openingFunnel`, `earlyExperience`, `continuationProxies`, `fallbackReopen`, `observation`, `inputQuality`, and aggregate `productModels`. Ratio objects contain `numerator`, `denominator`, and `rate` (null when denominator is zero). The file contains no user keys, conversation keys, queries, or replies. The independent verifier reconciles its metrics against detail rows.

IP/entity interest analysis belongs to `$analyze-teeni-interests` and consumes verified base packages. The old topic workflow is retired. Demand/result evaluation requires separately labeled evidence and must not be replaced by semantic guesses or automated self-confirmation.

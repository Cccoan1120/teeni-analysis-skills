import { createReadStream } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { StringDecoder } from "node:string_decoder";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { deepMerge, readJsonArgument, sha256, verifyManifest, validateFixedContract } from "./verification-support.mjs";

const FLAGS = new Set([
  "--workbook", "--primary-detail", "--ending-detail", "--manifest",
  "--primary-scene", "--baseline-detail", "--render-dir",
  "--expected-primary-rows", "--expected-ending-rows",
  "--rules", "--overrides",
  "--primary-source", "--baseline-source",
]);

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!FLAGS.has(flag) || !value) throw new Error(`Invalid argument: ${flag}`);
    values[flag] = value;
  }
  for (const required of ["--workbook", "--primary-detail", "--ending-detail", "--manifest", "--primary-scene"]) {
    if (!values[required]) throw new Error(`Missing required argument: ${required}`);
  }
  return values;
}

async function artifactTool() {
  const nodeModules = process.env.TEENI_NODE_MODULES;
  if (!nodeModules) throw new Error("TEENI_NODE_MODULES is required");
  const require = createRequire(import.meta.url);
  return import(pathToFileURL(require.resolve("@oai/artifact-tool", { paths: [nodeModules] })).href);
}

async function workbookZip() {
  const nodeModules = process.env.TEENI_NODE_MODULES;
  if (!nodeModules) throw new Error("TEENI_NODE_MODULES is required");
  const require = createRequire(import.meta.url);
  const JSZip = require(require.resolve("jszip", { paths: [nodeModules] }));
  return JSZip.loadAsync(await fs.readFile(workbookPath));
}

function hasFrozenHeader(xml) {
  const pane = xml.match(/<(?:\w+:)?pane\b[^>]*>/)?.[0] ?? "";
  return /\bySplit="1"/.test(pane)
    && /\btopLeftCell="A2"/.test(pane)
    && /\bstate="frozen"/.test(pane);
}

async function* csvRows(filePath) {
  const decoder = new StringDecoder("utf8");
  let row = [];
  let field = "";
  let quoted = false;
  let quotePending = false;
  let skipLf = false;

  function* consume(value) {
    for (const char of value) {
      if (skipLf) {
        skipLf = false;
        if (char === "\n") continue;
      }
      if (quoted) {
        if (quotePending) {
          if (char === '"') {
            field += '"';
            quotePending = false;
            continue;
          }
          quoted = false;
          quotePending = false;
        } else if (char === '"') {
          quotePending = true;
          continue;
        } else {
          field += char;
          continue;
        }
      }
      if (char === '"' && field === "") quoted = true;
      else if (char === ",") { row.push(field); field = ""; }
      else if (char === "\r" || char === "\n") {
        row.push(field);
        if (row.length > 1 || row[0] !== "") yield row;
        row = [];
        field = "";
        skipLf = char === "\r";
      } else field += char;
    }
  }

  for await (const chunk of createReadStream(filePath, { highWaterMark: 1024 * 1024 })) {
    yield* consume(decoder.write(chunk));
  }
  yield* consume(decoder.end());
  if (quotePending) {
    quoted = false;
    quotePending = false;
  }
  assert(!quoted, `CSV has an unterminated quoted field: ${path.basename(filePath)}`);
  if (field !== "" || row.length) {
    row.push(field);
    if (row.length > 1 || row[0] !== "") yield row;
  }
}

async function readCsv(filePath) {
  let headers = null;
  const rows = [];
  for await (const values of csvRows(filePath)) {
    if (!headers) {
      headers = values;
      headers[0] = headers[0]?.replace(/^\uFEFF/, "") ?? "";
      continue;
    }
    rows.push(Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""])));
  }
  return { headers: headers ?? [], rows };
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function normalizeFallbackText(value) {
  return String(value ?? "").normalize("NFKC").trim().toLowerCase()
    .replace(/[\s，。！？、,.!?；;：:“”"'‘’（）()《》【】\[\]~～\-_/]+/gu, "");
}

function normalizeInvalidText(value) {
  return String(value ?? "").trim().normalize("NFKC").toLowerCase()
    .replace(/[\s，。！？、,.!?；;：:“”"'‘’（）()《》【】\[\]~～\-_/]+/gu, "");
}

function parseBirthday(value) {
  const candidate = String(value ?? "").trim();
  if (!candidate) return { value: null, invalid: false };
  if (!/^\d{4}-\d{2}-\d{2}$/.test(candidate)) return { value: null, invalid: true };
  const [year, month, day] = candidate.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  const valid = year >= 1 && parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day;
  return valid ? { value: candidate, invalid: false } : { value: null, invalid: true };
}

function constellation(value) {
  if (!value) return "";
  const [, monthText, dayText] = value.split("-");
  const monthDay = Number(monthText) * 100 + Number(dayText);
  if (monthDay >= 1222 || monthDay <= 119) return "摩羯座";
  return [
    [218, "水瓶座"], [320, "双鱼座"], [419, "白羊座"], [520, "金牛座"],
    [621, "双子座"], [722, "巨蟹座"], [822, "狮子座"], [922, "处女座"],
    [1023, "天秤座"], [1122, "天蝎座"], [1221, "射手座"],
  ].find(([upper]) => monthDay <= upper)?.[1] ?? "";
}

function parseCity(value) {
  const raw = String(value ?? "");
  if (!raw.trim()) return { value: null, invalid: false };
  let candidate = raw.normalize("NFKC").trim();
  if (!candidate || candidate.length > 64 || /[\x00-\x1f{}\[\]<>]/u.test(candidate)) {
    return { value: null, invalid: true };
  }
  if (candidate.length > 1 && candidate.endsWith("市")) candidate = candidate.slice(0, -1);
  return { value: candidate, invalid: false };
}

function resolvedProfile(values, invalid) {
  if (values.size > 1) return { status: "冲突", value: "" };
  if (values.size === 1 && invalid) return { status: "含非法值", value: "" };
  if (values.size === 1) return { status: "正常", value: [...values][0] };
  return { status: invalid ? "非法" : "缺失", value: "" };
}

function invalidUserReason(value, rules) {
  if ((rules.opening.exact_texts ?? []).includes(String(value ?? "").trim())) return "系统占位开场模板";
  const normalized = normalizeInvalidText(value);
  if (!normalized) return "";
  const invalid = rules.invalid_turns;
  if (new RegExp(invalid.topic_opening_pattern).test(normalized)) return "话题占位开场模板";
  if (invalid.fixed_opening_prefixes.map(normalizeInvalidText).some((prefix) => prefix && normalized.startsWith(prefix))) return "固定开场前缀";
  if (new Set(invalid.exact_normalized_texts.map(normalizeInvalidText)).has(normalized)) return "确认无效内容";
  return "";
}

function splitSignals(value) {
  return String(value ?? "").split("|").filter(Boolean);
}

function safeCell(value) {
  return typeof value === "string" && /^[=+\-@]/.test(value) ? `'${value}` : value;
}

function combinedSafetySignals(row) {
  return [...new Set([...splitSignals(row.safety_signals), ...splitSignals(row.user_safety_signals)])].join("|");
}

function priorityUsers(issues, rules) {
  const grouped = new Map();
  for (const issue of issues) {
    if (!grouped.has(issue.row.clientId)) grouped.set(issue.row.clientId, []);
    grouped.get(issue.row.clientId).push(issue);
  }
  const config = rules.user_safety.priority;
  const p0 = new Set(config.p0_categories.map(String));
  const severityRank = new Map([["严重", 0], ["高风险", 1], ["高", 2], ["中", 3], ["待复核", 4]]);
  const priorityRank = new Map([["P0", 0], ["P1", 1], ["P2", 2]]);
  const result = [];
  for (const [clientId, values] of grouped) {
    const queryKeys = new Set(values.map((item) => item.queryKey));
    const sessions = new Set(values.map((item) => item.row.cid));
    const categories = new Set(values.map((item) => item.category));
    const p0Hits = [...categories].filter((value) => p0.has(value)).sort();
    let reviewPriority;
    let priorityReason;
    if (p0Hits.length) {
      reviewPriority = "P0";
      priorityReason = `命中P0类别：${p0Hits.join("、")}`;
    } else if (queryKeys.size >= Number(config.p1_min_hit_queries)
      || sessions.size >= Number(config.p1_min_hit_sessions)
      || categories.size >= Number(config.p1_min_categories)) {
      reviewPriority = "P1";
      const reasons = [];
      if (queryKeys.size >= Number(config.p1_min_hit_queries)) reasons.push(`累计${queryKeys.size}条query`);
      if (sessions.size >= Number(config.p1_min_hit_sessions)) reasons.push(`跨${sessions.size}个会话`);
      if (categories.size >= Number(config.p1_min_categories)) reasons.push(`涉及${categories.size}类风险`);
      priorityReason = reasons.join("；");
    } else {
      reviewPriority = "P2";
      priorityReason = "单次或低频高召回候选";
    }
    const ordered = values.slice().sort((a, b) => a.sourceTime.localeCompare(b.sourceTime));
    const highestSeverity = values.slice().sort((a, b) => (severityRank.get(a.severity) ?? 9) - (severityRank.get(b.severity) ?? 9))[0].severity;
    result.push({
      reviewPriority, clientId, hitQueries: queryKeys.size, hitSessions: sessions.size,
      categoryCount: categories.size, categories: [...categories].sort().join("|"),
      highestSeverity, firstHitTime: ordered[0].sourceTime,
      lastHitTime: ordered.at(-1).sourceTime, example: ordered[0].evidence, priorityReason,
    });
  }
  return result.sort((a, b) => (priorityRank.get(a.reviewPriority) - priorityRank.get(b.reviewPriority))
    || b.hitSessions - a.hitSessions || b.hitQueries - a.hitQueries
    || b.categoryCount - a.categoryCount || b.lastHitTime.localeCompare(a.lastHitTime)
    || a.clientId.localeCompare(b.clientId));
}

const DETAIL_REQUIRED = [
  "source_row", "id", "clientId", "cid", "sceneId", "timestamp", "created_at",
  "text", "ai_text", "parse_status", "intention", "subIntention", "turn_index",
  "session_turns", "is_template", "is_invalid_turn", "invalid_reason", "scoreable",
  "quality_signals", "quality_issue_count", "safety_signals", "safety_issue_count",
  "user_query_reviewable", "user_safety_signals", "user_safety_issue_count", "user_review_priority",
  "net_valid_turn_index", "session_net_valid_turns", "profile_age", "age_band",
  "profile_gender", "birthday", "city", "city_normalized", "constellation",
  "age_status", "gender_status", "birthday_status", "city_status", "profile_status",
];

function verifyDetailHeaders(headers, role) {
  for (const field of DETAIL_REQUIRED) assert(headers.includes(field), `${role} detail missing ${field}`);
  assert(!headers.includes("response"), `${role} detail must exclude response`);
  assert(!headers.some((field) => /topic|话题|semantic/i.test(field)), `${role} detail contains topic fields`);
}

function verifyDetailRow(row, sceneId, role, rowIndex) {
  assert(Number(row.source_row) === rowIndex + 2, `${role} source_row is not contiguous`);
  if (sceneId !== null) assert(row.sceneId === String(sceneId), `${role} sceneId mismatch`);
  assert(row.timestamp !== "", `${role} timestamp is empty`);
  assert(["是", "否"].includes(row.is_invalid_turn), `${role} invalid-turn state is illegal`);
  if (row.is_invalid_turn === "否") assert(Number(row.net_valid_turn_index) >= 1, `${role} valid row lacks net index`);
  if (row.profile_status === "正常") {
    assert(row.profile_age !== "" && row.profile_gender !== "", `${role} normal profile is incomplete`);
  }
  assert(["正常", "缺失", "非法", "含非法值", "冲突"].includes(row.birthday_status), `${role} birthday status is illegal`);
  assert(["正常", "缺失", "非法", "含非法值", "冲突"].includes(row.city_status), `${role} city status is illegal`);
  if (row.birthday_status !== "正常") assert(row.constellation === "", `${role} unresolved birthday has a constellation`);
  if (row.city_status !== "正常") assert(row.city_normalized === "", `${role} unresolved city has a normalized value`);
}

async function scanDetail(filePath, sceneId, role, onRow = () => {}) {
  let headers = null;
  let indexes = null;
  let rowCount = 0;
  const scenes = new Set();
  for await (const values of csvRows(filePath)) {
    if (!headers) {
      headers = values;
      headers[0] = headers[0]?.replace(/^\uFEFF/, "") ?? "";
      verifyDetailHeaders(headers, role);
      indexes = new Map(headers.map((header, index) => [header, index]));
      continue;
    }
    const row = Object.fromEntries(DETAIL_REQUIRED.map((field) => [field, values[indexes.get(field)] ?? ""]));
    verifyDetailRow(row, sceneId, role, rowCount);
    scenes.add(row.sceneId);
    onRow(row);
    rowCount += 1;
  }
  assert(headers, `${role} detail is empty`);
  return { headers, rowCount, scenes };
}

const args = parseArgs(process.argv.slice(2));
const workbookPath = path.resolve(args["--workbook"]);
const primaryDetailPath = path.resolve(args["--primary-detail"]);
const endingPath = path.resolve(args["--ending-detail"]);
const manifestPath = path.resolve(args["--manifest"]);
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
const effectiveRules = manifest.outputs?.filter((item) => item.role === "effective_rules") ?? [];
assert(effectiveRules.length === 1, "manifest must contain one effective_rules output");
assert(typeof effectiveRules[0].file === "string" && !/[\\/:]/.test(effectiveRules[0].file), "unsafe effective rules filename");
const packageRulesPath = path.resolve(path.dirname(manifestPath), effectiveRules[0].file);
assert(effectiveRules[0].sha256 === await sha256(packageRulesPath), "effective rules hash mismatch");
const rulesPath = path.resolve(args["--rules"] ?? packageRulesPath);
let rules = JSON.parse(await fs.readFile(rulesPath, "utf8"));
rules = deepMerge(rules, await readJsonArgument(args["--overrides"]));
assert(isDeepStrictEqual(rules, JSON.parse(await fs.readFile(packageRulesPath, "utf8"))), "verification rules differ from package effective rules");
assert(["13.0.0", "14.0.0", "15.0.0", "16.0.0"].includes(rules.rules_version), "verification rules version mismatch");
validateFixedContract(rules);
let safetyAudit = null;
if (["15.0.0", "16.0.0"].includes(rules.rules_version)) {
  const audited = spawnSync(process.env.TEENI_ANALYSIS_PYTHON, [path.join(scriptDir, "verify-safety.py")], {
    input: JSON.stringify({ manifest: manifestPath, source: args["--primary-source"], detail: primaryDetailPath }),
    encoding: "utf8", maxBuffer: 64 * 1024 * 1024, env: { ...process.env, PYTHONUTF8: "1" },
  });
  assert(audited.status === 0, `safety verification failed: ${audited.stderr}\n${audited.stdout}`);
  safetyAudit = JSON.parse(audited.stdout);
}
const auditedUserIssues = new Map((safetyAudit?.userIssues ?? []).map(([key, issues]) => [String(key), issues]));
const userRules = rules.user_safety.rules.map((item) => ({
  category: String(item.category), severity: String(item.severity), pattern: new RegExp(String(item.pattern)),
}));
const userIssues = [];
const reviewableUsers = new Set();
const actualPriorityByUser = new Map();
const sessions = new Map();
let reviewableRowCount = 0;
let aiSafetyCandidates = 0;
let scoreableAi = 0;
let exportTimestamp = 0;
const profileUsers = new Map();
const primary = await scanDetail(primaryDetailPath, args["--primary-scene"], "primary", (row) => {
  let profile = profileUsers.get(row.clientId);
  if (!profile) {
    profile = {
      birthdays: new Set(), birthdayInvalid: false, cities: new Set(), cityInvalid: false,
      birthdayStatuses: new Set(), cityStatuses: new Set(), cityValues: new Set(), constellations: new Set(),
    };
    profileUsers.set(row.clientId, profile);
  }
  const parsedBirthday = parseBirthday(row.birthday);
  if (parsedBirthday.value) profile.birthdays.add(parsedBirthday.value);
  profile.birthdayInvalid ||= parsedBirthday.invalid;
  const parsedCity = parseCity(row.city);
  if (parsedCity.value) profile.cities.add(parsedCity.value);
  profile.cityInvalid ||= parsedCity.invalid;
  profile.birthdayStatuses.add(row.birthday_status);
  profile.cityStatuses.add(row.city_status);
  profile.cityValues.add(row.city_normalized);
  profile.constellations.add(row.constellation);
  const expectedReviewable = Boolean(String(row.text ?? "").trim()) && !invalidUserReason(row.text, rules);
  assert(row.user_query_reviewable === (expectedReviewable ? "是" : "否"), `user query reviewable mismatch at source_row ${row.source_row}`);
  const matches = ["15.0.0", "16.0.0"].includes(rules.rules_version)
    ? (auditedUserIssues.get(row.source_row) ?? []).map((issue) => ({
      row, ...issue, sourceTime: String(row.created_at || row.timestamp),
      queryKey: `${row.cid}\u0000${row.id || `row-${row.source_row}`}\u0000${row.turn_index}`,
    }))
    : expectedReviewable ? userRules.flatMap((rule) => {
      const match = rule.pattern.exec(row.text);
      return match ? [{
        row, category: rule.category, severity: rule.severity, evidence: match[0],
        sourceTime: String(row.created_at || row.timestamp),
        queryKey: `${row.cid}\u0000${row.id || `row-${row.source_row}`}\u0000${row.turn_index}`,
      }] : [];
    })
    : [];
  const expectedSignals = matches.map((item) => `${item.category}(${item.severity})`).join("|");
  assert(row.user_safety_signals === expectedSignals, `user safety signals mismatch at source_row ${row.source_row}`);
  assert(Number(row.user_safety_issue_count) === matches.length, `user safety issue count mismatch at source_row ${row.source_row}`);
  if (expectedReviewable) {
    reviewableRowCount += 1;
    reviewableUsers.add(row.clientId);
  }
  userIssues.push(...matches);
  if (actualPriorityByUser.has(row.clientId)) {
    assert(actualPriorityByUser.get(row.clientId) === row.user_review_priority, `inconsistent user review priority for ${row.clientId}`);
  } else actualPriorityByUser.set(row.clientId, row.user_review_priority);
  aiSafetyCandidates += Number(row.safety_issue_count || 0);
  if (row.scoreable === "是") scoreableAi += 1;
  exportTimestamp = Math.max(exportTimestamp, Number(row.timestamp));

  const compact = {
    source_row: row.source_row, id: row.id, clientId: row.clientId, cid: row.cid,
    timestamp: row.timestamp, created_at: row.created_at, text: row.text, ai_text: row.ai_text,
    turn_index: row.turn_index, is_invalid_turn: row.is_invalid_turn, invalid_reason: row.invalid_reason,
    safety_signals: row.safety_signals, user_safety_signals: row.user_safety_signals,
    user_review_priority: row.user_review_priority,
    city_status: row.city_status, city_normalized: row.city_normalized,
    birthday_status: row.birthday_status, constellation: row.constellation,
  };
  let session = sessions.get(row.cid);
  if (!session) {
    session = { count: 0, validCount: 0, first: compact, last: compact, finalValid: null };
    sessions.set(row.cid, session);
  }
  session.count += 1;
  if (row.is_invalid_turn === "否") session.validCount += 1;
  if (Number(compact.turn_index) < Number(session.first.turn_index)) session.first = compact;
  if (Number(compact.turn_index) > Number(session.last.turn_index)) session.last = compact;
  if (row.is_invalid_turn === "否"
    && (!session.finalValid || Number(compact.turn_index) > Number(session.finalValid.turn_index))) {
    session.finalValid = compact;
  }
});
if (args["--expected-primary-rows"]) assert(primary.rowCount === Number(args["--expected-primary-rows"]), "primary row count mismatch");

const profileResolution = new Map();
for (const [clientId, profile] of profileUsers) {
  const birthday = resolvedProfile(profile.birthdays, profile.birthdayInvalid);
  const city = resolvedProfile(profile.cities, profile.cityInvalid);
  assert(profile.birthdayStatuses.size === 1 && profile.birthdayStatuses.has(birthday.status), `birthday resolution mismatch for ${clientId}`);
  assert(profile.cityStatuses.size === 1 && profile.cityStatuses.has(city.status), `city resolution mismatch for ${clientId}`);
  assert(profile.cityValues.size === 1 && profile.cityValues.has(city.value), `city normalized value mismatch for ${clientId}`);
  const expectedConstellation = birthday.status === "正常" ? constellation(birthday.value) : "";
  assert(profile.constellations.size === 1 && profile.constellations.has(expectedConstellation), `constellation mismatch for ${clientId}`);
  profileResolution.set(clientId, {
    birthdayStatus: birthday.status, cityStatus: city.status,
    city: city.value, constellation: expectedConstellation,
  });
}

const attentionUsers = priorityUsers(userIssues, rules);
const priorityByUser = new Map(attentionUsers.map((item) => [item.clientId, item.reviewPriority]));
for (const [clientId, actualPriority] of actualPriorityByUser) {
  assert(actualPriority === (priorityByUser.get(clientId) ?? ""), `user review priority mismatch for ${clientId}`);
}
const userRiskQueries = new Set(userIssues.map((item) => item.queryKey));

const ending = await readCsv(endingPath);
const endingRequired = ["cid", "净有效轮数", "末轮源行", "结束信号主类", "置信度", "分类证据", "末轮安全信号", "末轮AI安全信号", "末轮用户风险信号", "末轮用户复核优先级"];
for (const field of endingRequired) assert(ending.headers.includes(field), `ending detail missing ${field}`);
assert(!ending.headers.some((field) => /话题|topic/i.test(field)), "ending detail contains topic fields");
assert(new Set(ending.rows.map((row) => row.cid)).size === ending.rows.length, "ending cid is not unique");
for (const row of ending.rows) {
  assert(Number(row["净有效轮数"]) >= 5, "ending row is below five valid turns");
  const joined = sessions.get(row.cid)?.finalValid;
  assert(joined && joined.cid === row.cid && joined.is_invalid_turn === "否", "ending row does not join final valid turn");
  assert(joined.source_row === row["末轮源行"], "ending row is not the final valid turn");
  assert(row["末轮AI安全信号"] === joined.safety_signals, `ending AI safety mismatch for ${row.cid}`);
  assert(row["末轮用户风险信号"] === joined.user_safety_signals, `ending user safety mismatch for ${row.cid}`);
  assert(row["末轮安全信号"] === combinedSafetySignals(joined), `ending compatibility safety mismatch for ${row.cid}`);
  assert(row["末轮用户复核优先级"] === joined.user_review_priority, `ending user priority mismatch for ${row.cid}`);
}
if (args["--expected-ending-rows"]) assert(ending.rows.length === Number(args["--expected-ending-rows"]), "ending row count mismatch");

let baseline = null;
if (args["--baseline-detail"]) {
  baseline = await scanDetail(path.resolve(args["--baseline-detail"]), null, "baseline");
  assert(baseline.scenes.size === 1, "baseline contains multiple scenes");
}

assert(!JSON.stringify(manifest).includes(path.dirname(manifestPath)), "manifest contains an absolute output path");
assert(!JSON.stringify(manifest).match(/DASHSCOPE_API_KEY|Authorization|current_user_query/), "manifest contains semantic or credential data");
const outputRecords = new Map([
  ["workbook", { path: workbookPath }],
  ["primary_detail", { path: primaryDetailPath, rows: primary.rowCount }],
  ["ending_detail", { path: endingPath, rows: ending.rows.length }],
  ["effective_rules", { path: packageRulesPath }],
]);
const supplementalEntries = manifest.outputs.filter((item) => item.role === "supplemental_metrics");
assert(supplementalEntries.length === (["14.0.0", "15.0.0", "16.0.0"].includes(rules.rules_version) ? 1 : 0), "supplemental_metrics presence does not match package version");
let supplementalPath = null;
if (supplementalEntries.length) {
  assert(typeof supplementalEntries[0].file === "string" && !/[\\/:]/.test(supplementalEntries[0].file), "unsafe supplemental metrics filename");
  supplementalPath = path.resolve(path.dirname(manifestPath), supplementalEntries[0].file);
  const supplemental = JSON.parse(await fs.readFile(supplementalPath, "utf8"));
  assert(supplemental.coreVersion === manifest.coreVersion && supplemental.rulesVersion === manifest.rulesVersion && supplemental.sceneId === manifest.sceneId, "supplemental package metadata mismatch");
  assert(Boolean(supplemental.baseline) === Boolean(baseline), "supplemental baseline mode mismatch");
  outputRecords.set("supplemental_metrics", { path: supplementalPath });
}
if (baseline) outputRecords.set("baseline_detail", { path: path.resolve(args["--baseline-detail"]), rows: baseline.rowCount });
const structureEntries = manifest.outputs.filter((item) => item.role === "session_structure");
assert(structureEntries.length === (rules.rules_version === "16.0.0" ? 1 : 0), "session_structure presence does not match package version");
let structurePath = null;
if (structureEntries.length) {
  assert(typeof structureEntries[0].file === "string" && !/[\\/:]/.test(structureEntries[0].file), "unsafe session structure filename");
  structurePath = path.join(path.dirname(manifestPath), structureEntries[0].file);
  const structure = JSON.parse(await fs.readFile(structurePath, "utf8"));
  assert(structure.schemaVersion === "teeni-session-structure/1.0.0", "session structure schema mismatch");
  assert(structure.coreVersion === manifest.coreVersion && structure.rulesVersion === manifest.rulesVersion && structure.sceneId === manifest.sceneId && structure.dataDate === manifest.dataDate, "session structure metadata mismatch");
  assert(Boolean(structure.baseline) === Boolean(baseline), "session structure baseline mode mismatch");
  outputRecords.set("session_structure", { path: structurePath });
}
if (safetyAudit) {
  for (const role of ["safety_workbook", "safety_exclusions"]) {
    const entries = manifest.outputs.filter((item) => item.role === role);
    assert(entries.length === 1 && !/[\\/:]/.test(entries[0].file), `invalid ${role} artifact`);
    outputRecords.set(role, { path: path.join(path.dirname(manifestPath), entries[0].file) });
  }
}
const sourceRecords = new Map();
const sourceRequests = [];
for (const [role, detail, detailPath, sceneId] of [
  ["primary", primary, primaryDetailPath, String(args["--primary-scene"])],
  ...(baseline ? [["baseline", baseline, path.resolve(args["--baseline-detail"]), [...baseline.scenes][0]]] : []),
]) {
  const entries = manifest.sources?.filter((item) => item.role === role) ?? [];
  assert(entries.length === 1, `manifest must contain one ${role} source`);
  assert(typeof entries[0].file === "string" && !/[\\/:]/.test(entries[0].file), `unsafe ${role} source filename`);
  const source = path.resolve(args[`--${role}-source`] ?? path.join(path.dirname(manifestPath), entries[0].file));
  sourceRecords.set(role, { path: source, rows: detail.rowCount, sceneId });
  sourceRequests.push({ role, source, detail: detailPath, sceneId });
}
await verifyManifest(manifest, {
  sceneId: String(args["--primary-scene"]), rulesVersion: rules.rules_version,
  mode: baseline ? "comparison" : "primary", outputs: outputRecords, sources: sourceRecords,
});
assert(process.env.TEENI_ANALYSIS_PYTHON, "TEENI_ANALYSIS_PYTHON is required for independent source and workbook verification");
const packageAudit = spawnSync(process.env.TEENI_ANALYSIS_PYTHON, [path.join(scriptDir, "verify-package.py")], {
  input: JSON.stringify({ sources: sourceRequests, workbook: workbookPath, supplemental: supplementalPath, structure: structurePath, rules }),
  encoding: "utf8", env: { ...process.env, PYTHONUTF8: "1" }, maxBuffer: 4 * 1024 * 1024,
});
assert(packageAudit.status === 0, `source/workbook reconciliation failed: ${packageAudit.stderr || packageAudit.error || "unknown error"}`);
const audit = JSON.parse(packageAudit.stdout);
for (const [role, source] of sourceRecords) assert(audit.sources[role] === source.rows, `${role} source row count mismatch`);
assert(audit.formulaErrors.length === 0, `workbook contains error cells: ${JSON.stringify(audit.formulaErrors)}`);

const demographicMode = ["488", "901", "904"].includes(String(args["--primary-scene"]));
const expected = [
  "结论总览", "剔除单轮开场白后分析", "剔除无效轮次后分析", "五轮以上结束分析",
  "五轮以上结束分析（不剔除无效）", "五轮以上结束明细", "五轮以上结束明细（不剔除无效）",
  "有效对话子意图对比", "真实意图分析",
  ...(demographicMode ? ["年龄性别概览", "所在地分析", "星座分析"] : []),
  "新版问题总览", "新版开场分析", "新版开场明细", "新版质量问题", "新版安全问题",
  "需关注用户汇总", "用户风险表达明细",
  "优化指标体系", "产品迭代建议", "新版会话汇总", "分析口径",
];
if (baseline) expected.splice(1, 0, "版本基准对比");

const { SpreadsheetFile } = await artifactTool();
const workbook = await SpreadsheetFile.importXlsx(new Uint8Array(await fs.readFile(workbookPath)));
const sheetNames = workbook.worksheets.items.map((sheet) => sheet.name);
assert(JSON.stringify(sheetNames) === JSON.stringify(expected), `sheet order mismatch: ${JSON.stringify(sheetNames)}`);

function profileSegmentMetrics(kind) {
  const ordered = kind === "constellation"
    ? ["摩羯座", "水瓶座", "双鱼座", "白羊座", "金牛座", "双子座", "巨蟹座", "狮子座", "处女座", "天秤座", "天蝎座", "射手座"]
    : [];
  const groups = new Map(ordered.map((label) => [label, { users: new Set(), sessions: 0, turns: 0, fivePlus: 0 }]));
  const statusKey = kind === "city" ? "cityStatus" : "birthdayStatus";
  const valueKey = kind === "city" ? "city" : "constellation";
  const statuses = new Map();
  let normalUsers = 0;
  for (const [clientId, profile] of profileResolution) {
    statuses.set(profile[statusKey], (statuses.get(profile[statusKey]) ?? 0) + 1);
    if (profile[statusKey] !== "正常") continue;
    normalUsers += 1;
    if (!groups.has(profile[valueKey])) groups.set(profile[valueKey], { users: new Set(), sessions: 0, turns: 0, fivePlus: 0 });
    groups.get(profile[valueKey]).users.add(clientId);
  }
  for (const session of sessions.values()) {
    const status = kind === "city" ? session.first.city_status : session.first.birthday_status;
    const label = kind === "city" ? session.first.city_normalized : session.first.constellation;
    if (status !== "正常" || !label || session.validCount === 0) continue;
    const group = groups.get(label);
    group.sessions += 1;
    group.turns += session.validCount;
    group.fivePlus += Number(session.validCount >= 5);
  }
  const labels = ordered.length
    ? ordered
    : [...groups].sort((a, b) => b[1].users.size - a[1].users.size || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)).map(([label]) => label);
  return {
    users: profileResolution.size,
    normalUsers,
    coverage: normalUsers / Math.max(1, profileResolution.size),
    statuses,
    rows: labels.map((label) => {
      const group = groups.get(label);
      return {
        label, users: group.users.size, userShare: group.users.size / Math.max(1, normalUsers),
        sessions: group.sessions, turns: group.turns,
        average: group.turns / Math.max(1, group.sessions), fivePlus: group.fivePlus,
        fivePlusShare: group.fivePlus / Math.max(1, group.sessions),
      };
    }),
  };
}

function verifyProfileSheet(name, kind) {
  const metrics = profileSegmentMetrics(kind);
  const sheet = workbook.worksheets.items.find((item) => item.name === name);
  assert(sheet, `missing ${name}`);
  const values = sheet.getRange(`A1:I${metrics.users + metrics.statuses.size + 20}`).values;
  const kpi = values.find((row) => row[0] === "独立用户");
  assert(Number(kpi?.[1]) === metrics.users, `${name} user total mismatch`);
  assert(Number(kpi?.[3]) === metrics.normalUsers, `${name} normal-user total mismatch`);
  assert(Math.abs(Number(kpi?.[5]) - metrics.coverage) < 1e-12, `${name} coverage mismatch`);
  const statusHeader = values.findIndex((row) => row[0] === "解析状态");
  assert(statusHeader >= 0, `${name} status header is missing`);
  const actualStatuses = new Map();
  for (let index = statusHeader + 1; index < values.length && values[index][0] !== "" && values[index][0] !== null; index += 1) {
    actualStatuses.set(String(values[index][0]), Number(values[index][1]));
  }
  assert(JSON.stringify([...actualStatuses].sort()) === JSON.stringify([...metrics.statuses].sort()), `${name} status distribution mismatch`);
  const groupLabel = kind === "city" ? "城市" : "星座";
  const distributionHeader = values.findIndex((row) => row[0] === groupLabel);
  assert(distributionHeader >= 0, `${name} distribution header is missing`);
  const actualRows = values.slice(distributionHeader + 1, distributionHeader + 1 + metrics.rows.length);
  assert(actualRows.length === metrics.rows.length, `${name} distribution row count mismatch`);
  metrics.rows.forEach((expectedRow, index) => {
    const actual = actualRows[index];
    assert(String(actual[0]) === expectedRow.label, `${name} label mismatch at row ${index + 1}`);
    const numbers = [expectedRow.users, expectedRow.userShare, expectedRow.sessions, expectedRow.turns, expectedRow.average, expectedRow.fivePlus, expectedRow.fivePlusShare];
    numbers.forEach((expectedValue, numberIndex) => {
      assert(Math.abs(Number(actual[numberIndex + 1]) - expectedValue) < 1e-12, `${name} metric mismatch for ${expectedRow.label}`);
    });
    assert(String(actual[8]) === (expectedRow.users >= 30 ? "可描述" : "样本不足"), `${name} sample label mismatch for ${expectedRow.label}`);
  });
  assert(sheet.charts.items.length >= 1, `${name} lacks a chart`);
  assert(sheet.tables.items.length === 1, `${name} lacks the complete filterable table`);
  return metrics;
}

let locationMetrics = null;
let constellationMetrics = null;
if (demographicMode) {
  locationMetrics = verifyProfileSheet("所在地分析", "city");
  constellationMetrics = verifyProfileSheet("星座分析", "constellation");
}
const endingSheet = workbook.worksheets.items.find((sheet) => sheet.name === "五轮以上结束明细");
const rawEndingSheet = workbook.worksheets.items.find((sheet) => sheet.name === "五轮以上结束明细（不剔除无效）");
const rawEndingAnalysisSheet = workbook.worksheets.items.find((sheet) => sheet.name === "五轮以上结束分析（不剔除无效）");
const attentionSheet = workbook.worksheets.items.find((sheet) => sheet.name === "需关注用户汇总");
const userRiskSheet = workbook.worksheets.items.find((sheet) => sheet.name === "用户风险表达明细");
assert(endingSheet.tables.items.length === 1, "valid ending sheet lacks a filterable table");
assert(rawEndingSheet.tables.items.length === 1, "raw ending sheet lacks a filterable table");
assert(attentionSheet.tables.items.length === 1, "attention-user sheet lacks a filterable table");
assert(userRiskSheet.tables.items.length === 1, "user-risk sheet lacks a filterable table");
const zip = await workbookZip();
for (const [sheetName, label] of [
  [endingSheet.name, "valid ending"], [rawEndingSheet.name, "raw ending"],
  [attentionSheet.name, "attention user"], [userRiskSheet.name, "user risk"],
]) {
  const sheetIndex = sheetNames.indexOf(sheetName) + 1;
  const worksheet = zip.file(`xl/worksheets/sheet${sheetIndex}.xml`);
  assert(worksheet, `${label} ending worksheet XML is missing`);
  assert(hasFrozenHeader(await worksheet.async("string")), `${label} ending sheet does not freeze the header row`);
}
const validHeaders = endingSheet.getRange("A1:X1").values[0];
const rawHeaders = rawEndingSheet.getRange("A1:AH1").values[0];
assert(!validHeaders.some((field) => /^response$/i.test(String(field))), "valid ending sheet contains raw response JSON");
for (const field of ["末轮安全信号", "末轮AI安全信号", "末轮用户风险信号", "末轮用户复核优先级"]) {
  assert(validHeaders.includes(field), `valid ending sheet lacks ${field}`);
  assert(rawHeaders.includes(field), `raw ending sheet lacks ${field}`);
}
const validHeaderMap = new Map(validHeaders.map((header, index) => [String(header), index]));
const validWorkbookRows = ending.rows.length
  ? endingSheet.getRange(`A2:X${ending.rows.length + 1}`).values : [];
assert(validWorkbookRows.length === ending.rows.length, "valid ending workbook row count mismatch");
for (let index = 0; index < ending.rows.length; index += 1) {
  const workbookRow = validWorkbookRows[index];
  const csvRow = ending.rows[index];
  for (const field of ["cid", "末轮源行", "末轮安全信号", "末轮AI安全信号", "末轮用户风险信号", "末轮用户复核优先级"]) {
    assert(String(workbookRow[validHeaderMap.get(field)] ?? "") === String(csvRow[field] ?? ""), `valid ending workbook mismatch for ${field} at row ${index + 2}`);
  }
}
assert(rawHeaders.includes("末轮是否无效") && rawHeaders.includes("末轮无效原因"), "raw ending sheet lacks invalid-turn audit fields");
for (const field of [
  "是否命中对/嗯固定兜底", "触发词", "固定兜底类型", "固定兜底文本", "下一会话cid",
  "下一会话开始时间", "间隔秒数", "5分钟内新开会话",
]) assert(rawHeaders.includes(field), `raw ending sheet lacks fallback-reopen field ${field}`);
assert(!rawHeaders.some((field) => /^response$/i.test(String(field))), "raw ending sheet contains raw response JSON");

const attentionHeaders = attentionSheet.getRange("A1:K1").values[0].map(String);
assert(JSON.stringify(attentionHeaders) === JSON.stringify([
  "优先级", "clientId", "命中query数", "命中会话数", "类别数", "类别集合", "最高严重度",
  "首次命中时间", "最后命中时间", "典型证据", "分级原因",
]), "attention-user headers mismatch");
const attentionWorkbookRows = attentionUsers.length
  ? attentionSheet.getRange(`A2:K${attentionUsers.length + 1}`).values : [];
assert(attentionWorkbookRows.length === attentionUsers.length, "attention-user row count mismatch");
for (let index = 0; index < attentionUsers.length; index += 1) {
  const actual = attentionWorkbookRows[index].map((value) => String(value ?? ""));
  const item = attentionUsers[index];
  const expectedRow = [item.reviewPriority, item.clientId, item.hitQueries, item.hitSessions, item.categoryCount,
    item.categories, item.highestSeverity, item.firstHitTime, item.lastHitTime, item.example, item.priorityReason]
    .map((value) => String(safeCell(value)));
  assert(JSON.stringify(actual) === JSON.stringify(expectedRow), `attention-user row mismatch at ${index + 2}`);
}
const attentionNext = attentionSheet.getRange(`A${attentionUsers.length + 2}:K${attentionUsers.length + 2}`).values[0];
assert(attentionNext.every((value) => value === "" || value === null), "attention-user sheet has extra rows");

const userRiskHeaders = userRiskSheet.getRange("A1:M1").values[0].map(String);
assert(JSON.stringify(userRiskHeaders) === JSON.stringify([
  "优先级", "类别", "严重度", "id", "clientId", "cid", "时间", "轮次", "证据", "用户query",
  "对应AI response", "AI安全信号", "待复核状态",
]), "user-risk headers mismatch");
const userRiskWorkbookRows = userIssues.length
  ? userRiskSheet.getRange(`A2:M${userIssues.length + 1}`).values : [];
assert(userRiskWorkbookRows.length === userIssues.length, "user-risk row count mismatch");
const expectedUserRiskRows = new Map();
for (const issue of userIssues) {
  const expectedRow = [priorityByUser.get(issue.row.clientId) ?? "", issue.category, issue.severity,
    issue.row.id || `row-${issue.row.source_row}`, issue.row.clientId, issue.row.cid, issue.sourceTime,
    issue.row.turn_index, issue.evidence, issue.row.text, issue.row.ai_text, issue.row.safety_signals,
    "疑似待复核"].map((value) => String(safeCell(value)));
  const key = [expectedRow[3], expectedRow[4], expectedRow[5], expectedRow[7], expectedRow[1]].join("\u0000");
  assert(!expectedUserRiskRows.has(key), `duplicate expected user-risk key: ${key}`);
  expectedUserRiskRows.set(key, expectedRow);
}
const seenUserRiskKeys = new Set();
for (let index = 0; index < userRiskWorkbookRows.length; index += 1) {
  const actual = userRiskWorkbookRows[index].map((value) => String(value ?? ""));
  const key = [actual[3], actual[4], actual[5], actual[7], actual[1]].join("\u0000");
  const expectedRow = expectedUserRiskRows.get(key);
  assert(expectedRow, `unexpected user-risk detail key at row ${index + 2}`);
  assert(!seenUserRiskKeys.has(key), `duplicate user-risk detail key at row ${index + 2}`);
  seenUserRiskKeys.add(key);
  assert(JSON.stringify(actual) === JSON.stringify(expectedRow), `user-risk detail value mismatch at row ${index + 2}`);
}
assert(seenUserRiskKeys.size === expectedUserRiskRows.size, "user-risk detail is incomplete");
const userRiskNext = userRiskSheet.getRange(`A${userIssues.length + 2}:M${userIssues.length + 2}`).values[0];
assert(userRiskNext.every((value) => value === "" || value === null), "user-risk sheet has extra rows");

const summaryRows = workbook.worksheets.items.find((sheet) => sheet.name === "结论总览").getRange("A1:L24").values;
const summaryMetric = (label) => summaryRows.find((row) => row[8] === label);
const profileSummaryMetric = (label) => summaryRows.find((row) => row[0] === label);
assert(Number(summaryMetric("AI安全候选")?.[9]) === aiSafetyCandidates, "summary AI safety candidate count mismatch");
assert(Math.abs(Number(summaryMetric("AI安全候选")?.[10]) - aiSafetyCandidates / Math.max(1, scoreableAi)) < 1e-12, "summary AI safety rate mismatch");
assert(Number(summaryMetric("用户风险表达候选")?.[9]) === userRiskQueries.size, "summary user-risk query count mismatch");
assert(Math.abs(Number(summaryMetric("用户风险表达候选")?.[10]) - userRiskQueries.size / Math.max(1, reviewableRowCount)) < 1e-12, "summary user-risk rate mismatch");
assert(Number(summaryMetric("需关注用户候选")?.[9]) === attentionUsers.length, "summary attention-user count mismatch");
assert(Math.abs(Number(summaryMetric("需关注用户候选")?.[10]) - attentionUsers.length / Math.max(1, reviewableUsers.size)) < 1e-12, "summary attention-user rate mismatch");
if (rules.rules_version === "16.0.0") {
  if (!reviewableRowCount) assert([null, ""].includes(summaryMetric("用户风险表达候选")?.[10]), "empty user-risk denominator must be blank");
  if (!reviewableUsers.size) assert([null, ""].includes(summaryMetric("需关注用户候选")?.[10]), "empty attention-user denominator must be blank");
  const structure = JSON.parse(await fs.readFile(structurePath, "utf8")).primary;
  const overview = workbook.worksheets.items.find((sheet) => sheet.name === "结论总览").getRange("A1:L100").values;
  for (const [label, numerator, denominator, metric] of [
    ["平均会话轮次", "totalTurns", "totalSessions", "averageTurns"],
    ["多轮会话率", "multiTurnSessions", "totalSessions", "multiTurnRate"],
    ["多轮会话平均轮次", "multiTurnTurns", "multiTurnSessions", "multiTurnAverageTurns"],
    ["5轮及以上会话占比", "fivePlusSessions", "totalSessions", "fivePlusRate"],
  ]) {
    const row = overview.find((item) => item[0] === label);
    assert(row && row[4] === structure[numerator] && row[5] === structure[denominator], `overview structure counts mismatch: ${label}`);
    assert(structure[metric] === null ? [null, ""].includes(row[6]) : Math.abs(row[6] - structure[metric]) < 1e-12, `overview structure value mismatch: ${label}`);
  }
}
const priorityCounts = new Map([["P0", 0], ["P1", 0], ["P2", 0]]);
for (const item of attentionUsers) priorityCounts.set(item.reviewPriority, priorityCounts.get(item.reviewPriority) + 1);
assert(String(summaryMetric("P0 / P1 / P2")?.[9]) === `${priorityCounts.get("P0")} / ${priorityCounts.get("P1")} / ${priorityCounts.get("P2")}`, "summary priority distribution mismatch");
if (demographicMode) {
  assert(Math.abs(Number(profileSummaryMetric("城市覆盖率")?.[1]) - locationMetrics.coverage) < 1e-12, "summary city coverage mismatch");
  assert(Math.abs(Number(profileSummaryMetric("生日可解析率")?.[1]) - constellationMetrics.coverage) < 1e-12, "summary birthday coverage mismatch");
}

const issueOverviewHeaders = workbook.worksheets.items.find((sheet) => sheet.name === "新版问题总览").getRange("A4:J4").values[0].map(String);
assert(issueOverviewHeaders[0] === "审核对象", "issue overview lacks review target column");
const rawEndingRows = [...sessions.values()]
  .filter((session) => session.count >= 5)
  .map((session) => session.last);
assert(rawEndingRows.length >= ending.rows.length, "raw ending cohort is smaller than valid cohort");
if (rawEndingRows.length) {
  const workbookLastRow = rawEndingSheet.getRange(`A${rawEndingRows.length + 1}:AH${rawEndingRows.length + 1}`).values[0];
  assert(workbookLastRow.some((value) => value !== ""), "raw ending sheet row count is short");
  const nextRow = rawEndingSheet.getRange(`A${rawEndingRows.length + 2}:AH${rawEndingRows.length + 2}`).values[0];
  assert(nextRow.every((value) => value === "" || value === null), "raw ending sheet row count is long");
  const rawHeaderMap = new Map(rawHeaders.map((header, index) => [String(header), index]));
  const rawWorkbookRows = rawEndingSheet.getRange(`A2:AH${rawEndingRows.length + 1}`).values;
  const rawByCid = new Map(rawWorkbookRows.map((row) => [String(row[rawHeaderMap.get("cid")]), row]));
  const fallbackConfig = rules.ending_analysis.fallback_reopen;
  const fixedReplies = new Map(fallbackConfig.fixed_replies.map((item) => [normalizeFallbackText(item.text), String(item.type)]));
  const triggerQueries = new Set(fallbackConfig.trigger_queries.map(normalizeFallbackText));
  const sessionStartsByUser = new Map();
  for (const session of sessions.values()) {
    if (!sessionStartsByUser.has(session.first.clientId)) sessionStartsByUser.set(session.first.clientId, []);
    sessionStartsByUser.get(session.first.clientId).push(session.first);
  }
  for (const starts of sessionStartsByUser.values()) {
    starts.sort((left, right) => Number(left.timestamp) - Number(right.timestamp)
      || String(left.created_at).localeCompare(String(right.created_at))
      || Number(left.source_row) - Number(right.source_row));
  }
  let fallbackTriggers = 0;
  let fallbackReopened = 0;
  const fallbackTriggerRows = [];
  const fallbackTriggerUsers = new Set();
  const fallbackReopenedUsers = new Set();
  for (const finalRaw of rawEndingRows) {
    const workbookRow = rawByCid.get(finalRaw.cid);
    assert(workbookRow, `raw ending sheet missing cid ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("末轮源行")] ?? "") === finalRaw.source_row, `raw ending source mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("末轮是否无效")] ?? "") === finalRaw.is_invalid_turn, `raw ending invalid state mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("末轮无效原因")] ?? "") === finalRaw.invalid_reason, `raw ending invalid reason mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("末轮AI安全信号")] ?? "") === finalRaw.safety_signals, `raw ending AI safety mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("末轮用户风险信号")] ?? "") === finalRaw.user_safety_signals, `raw ending user safety mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("末轮安全信号")] ?? "") === combinedSafetySignals(finalRaw), `raw ending compatibility safety mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("末轮用户复核优先级")] ?? "") === finalRaw.user_review_priority, `raw ending user priority mismatch for ${finalRaw.cid}`);
    const query = normalizeFallbackText(finalRaw.text);
    const fallbackType = fixedReplies.get(normalizeFallbackText(finalRaw.ai_text));
    const triggered = triggerQueries.has(query) && Boolean(fallbackType);
    assert(String(workbookRow[rawHeaderMap.get("是否命中对/嗯固定兜底")] ?? "") === (triggered ? "是" : "否"), `fallback trigger mismatch for ${finalRaw.cid}`);
    if (!triggered) continue;
    fallbackTriggers += 1;
    fallbackTriggerUsers.add(finalRaw.clientId);
    const next = (sessionStartsByUser.get(finalRaw.clientId) ?? [])
      .find((row) => row.cid !== finalRaw.cid && Number(row.timestamp) > Number(finalRaw.timestamp));
    const delay = next ? Number(next.timestamp) - Number(finalRaw.timestamp) : null;
    const reopened = delay !== null && delay > 0 && delay <= 300;
    assert(String(workbookRow[rawHeaderMap.get("触发词")] ?? "") === query, `fallback query mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("固定兜底类型")] ?? "") === fallbackType, `fallback type mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("固定兜底文本")] ?? "") === finalRaw.ai_text, `fallback text mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("下一会话cid")] ?? "") === (next?.cid ?? ""), `fallback next cid mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("下一会话开始时间")] ?? "") === (next?.created_at ?? ""), `fallback next start mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("间隔秒数")] ?? "") === (delay ?? "").toString(), `fallback delay mismatch for ${finalRaw.cid}`);
    assert(String(workbookRow[rawHeaderMap.get("5分钟内新开会话")] ?? "") === (reopened ? "是" : "否"), `fallback reopen mismatch for ${finalRaw.cid}`);
    fallbackTriggerRows.push({ query, delay, finalTimestamp: Number(finalRaw.timestamp) });
    if (reopened) {
      fallbackReopened += 1;
      fallbackReopenedUsers.add(finalRaw.clientId);
    }
  }
  globalThis.fallbackReopenVerification = {
    fallbackTriggers, fallbackTriggerUsers: fallbackTriggerUsers.size,
    fallbackReopened, fallbackReopenedUsers: fallbackReopenedUsers.size,
  };
  const analysisRows = rawEndingAnalysisSheet.getRange("A1:H120").values;
  const proxyNote = analysisRows.find((row) => String(row[0]).startsWith("同一用户在原始末轮命中"));
  assert(String(proxyNote?.[0] ?? "").includes("行为代理") && String(proxyNote?.[0] ?? "").includes("不等于真实动机"), "fallback proxy limitation is missing");
  const summaryRow = analysisRows.find((row) => row[0] === "触发会话");
  const rateRow = analysisRows.find((row) => row[0] === (["14.0.0", "15.0.0", "16.0.0"].includes(rules.rules_version) ? "已观察会话新开比例" : "会话级新开率"));
  assert(Number(summaryRow?.[1]) === fallbackTriggers, "fallback analysis trigger-session total mismatch");
  assert(Number(summaryRow?.[3]) === fallbackTriggerUsers.size, "fallback analysis trigger-user total mismatch");
  assert(Number(summaryRow?.[5]) === fallbackReopened, "fallback analysis reopened-session total mismatch");
  assert(Number(summaryRow?.[7]) === fallbackReopenedUsers.size, "fallback analysis reopened-user total mismatch");
  assert(Math.abs(Number(rateRow?.[1]) - fallbackReopened / Math.max(1, fallbackTriggers)) < 1e-12, "fallback analysis session rate mismatch");
  assert(Math.abs(Number(rateRow?.[3]) - fallbackReopenedUsers.size / Math.max(1, fallbackTriggerUsers.size)) < 1e-12, "fallback analysis user rate mismatch");
  const bands = [
    ["1分钟内", 0, 60], ["1-2分钟", 60, 120], ["2-3分钟", 120, 180],
    ["3-5分钟", 180, 300], ["5-10分钟", 300, 600], ["10-30分钟", 600, 1800],
    ["30-60分钟", 1800, 3600], ["1-3小时", 3600, 10800], ["3-6小时", 10800, 21600],
    ["6-24小时", 21600, 86400], ["24小时以上", 86400, null],
  ];
  const observedRows = fallbackTriggerRows.filter((row) => row.delay !== null);
  const distributionHeader = analysisRows.findIndex((row) => row[0] === "新开间隔");
  assert(distributionHeader >= 0, "fallback delay distribution header is missing");
  const distributionRows = analysisRows.slice(distributionHeader + 1, distributionHeader + 1 + bands.length + 1);
  let distributionTotal = 0;
  let observedRateTotal = 0;
  for (const [index, [label, lower, upper]] of bands.entries()) {
    const matched = observedRows.filter((row) => row.delay > lower && (upper === null || row.delay <= upper));
    const workbookBand = distributionRows[index];
    assert(workbookBand[0] === label, `fallback delay label mismatch for ${label}`);
    assert(Number(workbookBand[1]) === matched.filter((row) => row.query === "对").length, `fallback 对 count mismatch for ${label}`);
    assert(Number(workbookBand[2]) === matched.filter((row) => row.query === "嗯").length, `fallback 嗯 count mismatch for ${label}`);
    assert(Number(workbookBand[3]) === matched.length, `fallback total count mismatch for ${label}`);
    assert(Number(workbookBand[1]) + Number(workbookBand[2]) === Number(workbookBand[3]), `fallback query split mismatch for ${label}`);
    assert(Math.abs(Number(workbookBand[4]) - matched.length / Math.max(1, fallbackTriggers)) < 1e-12, `fallback trigger share mismatch for ${label}`);
    assert(Math.abs(Number(workbookBand[5]) - matched.length / Math.max(1, observedRows.length)) < 1e-12, `fallback observed share mismatch for ${label}`);
    if (upper === null) {
      assert(workbookBand[6] === "" || workbookBand[6] === null, "open delay band must not have an observable denominator");
    } else {
      const observable = fallbackTriggerRows.filter((row) => (row.delay !== null && row.delay <= upper) || exportTimestamp - row.finalTimestamp >= upper).length;
      assert(Number(workbookBand[6]) === observable, `fallback observable sample mismatch for ${label}`);
      assert(Number(workbookBand[6]) <= fallbackTriggers, `fallback observable sample exceeds triggers for ${label}`);
    }
    distributionTotal += matched.length;
    observedRateTotal += Number(workbookBand[5]);
  }
  const noNext = fallbackTriggerRows.filter((row) => row.delay === null);
  const noNextWorkbook = distributionRows.at(-1);
  assert(noNextWorkbook[0] === "截至导出未观察到下一会话", "fallback no-next label mismatch");
  assert(Number(noNextWorkbook[1]) === noNext.filter((row) => row.query === "对").length, "fallback no-next 对 count mismatch");
  assert(Number(noNextWorkbook[2]) === noNext.filter((row) => row.query === "嗯").length, "fallback no-next 嗯 count mismatch");
  assert(Number(noNextWorkbook[3]) === noNext.length, "fallback no-next total mismatch");
  assert(noNextWorkbook[5] === "" || noNextWorkbook[5] === null, "fallback no-next observed share must be blank");
  assert(noNextWorkbook[6] === "" || noNextWorkbook[6] === null, "fallback no-next observable denominator must be blank");
  distributionTotal += noNext.length;
  assert(distributionTotal === fallbackTriggers, "fallback distribution does not partition trigger sessions");
  assert(Math.abs(observedRateTotal - (observedRows.length ? 1 : 0)) < 1e-10, "fallback observed shares do not sum to 100%");
  assert(rawEndingAnalysisSheet.charts.items.length >= 2, "raw ending analysis lacks the fallback delay chart");
}

const verificationStats = {
  primaryRows: primary.rowCount,
  endingRows: ending.rows.length,
  rawEndingRows: rawEndingRows.length,
  reviewableUserQueries: reviewableRowCount,
  userRiskExpressions: userRiskQueries.size,
  userRiskRuleHits: userIssues.length,
  aiSafetyCandidates,
  attentionUsers: attentionUsers.length,
  attentionP0: priorityCounts.get("P0"),
  attentionP1: priorityCounts.get("P1"),
  attentionP2: priorityCounts.get("P2"),
  locationNormalUsers: locationMetrics?.normalUsers ?? 0,
  birthdayNormalUsers: constellationMetrics?.normalUsers ?? 0,
};

ending.rows.length = 0;
rawEndingRows.length = 0;
userIssues.length = 0;
validWorkbookRows.length = 0;
attentionWorkbookRows.length = 0;
userRiskWorkbookRows.length = 0;
sessions.clear();
actualPriorityByUser.clear();
reviewableUsers.clear();
userRiskQueries.clear();
expectedUserRiskRows.clear();
seenUserRiskKeys.clear();
profileUsers.clear();
profileResolution.clear();

const renderDir = path.resolve(args["--render-dir"] ?? path.join(path.dirname(workbookPath), `${path.basename(workbookPath, ".xlsx")}_renders`));
await fs.mkdir(renderDir, { recursive: true });
for (const name of sheetNames) {
  const previewColumns = new Map([
    ["五轮以上结束明细", "X"],
    ["五轮以上结束明细（不剔除无效）", "AH"],
    ["新版开场明细", "L"],
    ["新版质量问题", "H"],
    ["新版安全问题", "H"],
    ["需关注用户汇总", "K"],
    ["用户风险表达明细", "M"],
    ["新版会话汇总", "L"],
  ]);
  const previewColumn = previewColumns.get(name);
  const rendered = await workbook.render({
    sheetName: name,
    ...(previewColumn ? { range: `A1:${previewColumn}61` } : { autoCrop: "all" }),
    scale: 1,
    format: "png",
  });
  const bytes = new Uint8Array(await rendered.arrayBuffer());
  assert(bytes.length > 500, `render is blank for ${name}`);
  await fs.writeFile(path.join(renderDir, `${name.replace(/[<>:"/\\|?*]/g, "-")}.png`), bytes);
}

if (safetyAudit) {
  const entry = manifest.outputs.find((item) => item.role === "safety_workbook");
  const rendered = spawnSync(process.execPath, [path.join(scriptDir, "render-safety-review.mjs"),
    "--workbook", path.join(path.dirname(manifestPath), entry.file), "--output-dir", path.join(renderDir, "safety-review")], {
    encoding: "utf8", maxBuffer: 16 * 1024 * 1024, env: process.env,
  });
  assert(rendered.status === 0, `safety rendering failed: ${rendered.stderr}\n${rendered.stdout}`);
}

console.log(JSON.stringify({
  workbook: path.basename(workbookPath),
  sheets: sheetNames.length,
  ...verificationStats,
  ...(globalThis.fallbackReopenVerification ?? {
    fallbackTriggers: 0, fallbackTriggerUsers: 0, fallbackReopened: 0, fallbackReopenedUsers: 0,
  }),
  formulaErrors: 0,
  renderedSheets: sheetNames.length,
  manifestVerified: true,
  ...(safetyAudit ? { safetyReviewVerified: true, safetySheets: 6, gameExcludedHits: safetyAudit.excludedHits } : {}),
}));

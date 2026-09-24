import { spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { finalizeRun, reserveOutputPath } from "./output-reservation.mjs";
import { deepMerge, readJsonArgument, validateFixedContract } from "./verification-support.mjs";

const VALUE_FLAGS = new Set([
  "--primary", "--primary-scene", "--baseline", "--baseline-scene",
  "--primary-label", "--baseline-label", "--output-dir", "--overrides",
  "--data-date",
]);

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    if (!VALUE_FLAGS.has(flag)) throw new Error(`Unknown argument: ${flag}`);
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`${flag} requires a value`);
    values[flag] = value;
    index += 1;
  }
  for (const required of ["--primary", "--primary-scene", "--data-date"]) {
    if (!values[required]) throw new Error(`Missing required argument: ${required}`);
  }
  if (Boolean(values["--baseline"]) !== Boolean(values["--baseline-scene"])) {
    throw new Error("--baseline and --baseline-scene must be supplied together");
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(values["--data-date"]) || new Date(`${values["--data-date"]}T00:00:00Z`).toISOString().slice(0, 10) !== values["--data-date"]) {
    throw new Error("--data-date must be a valid YYYY-MM-DD business date");
  }
  return values;
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    maxBuffer: 128 * 1024 * 1024,
    ...options,
  });
  if (result.status !== 0) {
    throw new Error(`${path.basename(command)} failed\nSTDOUT:\n${result.stdout}\nSTDERR:\n${result.stderr}`);
  }
  return result.stdout;
}

function renderNodeOptions(current = "") {
  if (/--max-old-space-size(?:=|\s)/.test(current)) return current;
  return `${current} --max-old-space-size=6144`.trim();
}

function dateStamp(date = new Date()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(date).replaceAll("-", "");
}

function safeLabel(value) {
  return String(value).trim().replace(/[<>:"/\\|?*\x00-\x1F]+/g, "-").slice(0, 48) || "版本";
}

async function reserve(filePath) {
  const handle = await fs.open(filePath, "wx");
  await handle.close();
  return filePath;
}

async function sha256(filePath) {
  const digest = crypto.createHash("sha256");
  digest.update(await fs.readFile(filePath));
  return digest.digest("hex");
}

async function outputRecord(role, filePath, rows = undefined) {
  return {
    role,
    file: path.basename(filePath),
    sha256: await sha256(filePath),
    ...(rows === undefined ? {} : { rows }),
  };
}

const values = parseArgs(process.argv.slice(2));
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const primary = path.resolve(values["--primary"]);
const baseline = values["--baseline"] ? path.resolve(values["--baseline"]) : null;
const outputDir = path.resolve(values["--output-dir"] ?? path.dirname(primary));
const primaryLabel = values["--primary-label"] ?? "新版";
let outputPath;
let primaryDetailPath;
let baselineDetailPath;
let endingDetailPath;
let manifestPath;
let effectiveRulesPath;
let supplementalMetricsPath;
let sessionStructurePath;
let safetyWorkbookPath;
let safetyExclusionsPath;
let tempDir;
let completed = false;
let primaryError;
const startedAt = Date.now();

try {
  outputPath = await reserveOutputPath(outputDir, `Teeni基础分析_${dateStamp()}`);
  const stem = path.basename(outputPath, ".xlsx");
  primaryDetailPath = await reserve(path.join(outputDir, `${stem}_${safeLabel(primaryLabel)}_基础逐轮明细.csv`));
  endingDetailPath = await reserve(path.join(outputDir, `${stem}_${safeLabel(primaryLabel)}_五轮以上结束明细.csv`));
  manifestPath = await reserve(path.join(outputDir, `${stem}_基础分析清单.json`));
  effectiveRulesPath = await reserve(path.join(outputDir, `${stem}_effective-rules.json`));
  supplementalMetricsPath = await reserve(path.join(outputDir, `${stem}_supplemental-metrics.json`));
  sessionStructurePath = await reserve(path.join(outputDir, `${stem}_session-structure.json`));
  const product = ({ 488: "M1", 904: "M2", 901: "M2" })[values["--primary-scene"]] ?? safeLabel(values["--primary-scene"]);
  safetyWorkbookPath = await reserveOutputPath(outputDir, `Teeni安全复核_${values["--data-date"].replaceAll("-", "")}_${product}`);
  safetyExclusionsPath = await reserve(path.join(outputDir, `${path.basename(safetyWorkbookPath, ".xlsx")}_游戏排除证据.json`));
  if (baseline) {
    baselineDetailPath = await reserve(path.join(
      outputDir,
      `${stem}_${safeLabel(values["--baseline-label"] ?? "基准版")}_基础逐轮明细.csv`,
    ));
  }

  const python = process.env.TEENI_ANALYSIS_PYTHON;
  if (!python) throw new Error("TEENI_ANALYSIS_PYTHON must point to the loaded workspace Python executable");
  tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "teeni-base-report-"));
  const reportPath = path.join(tempDir, "report.json");
  const rulesPath = path.resolve(scriptDir, "../references/default-rules.json");
  const effectiveRules = deepMerge(await readJsonArgument(rulesPath), await readJsonArgument(values["--overrides"]));
  validateFixedContract(effectiveRules);
  await fs.writeFile(effectiveRulesPath, `${JSON.stringify(effectiveRules, null, 2)}\n`, "utf8");
  const pythonPath = [path.resolve(scriptDir, "../vendor"), process.env.PYTHONPATH]
    .filter(Boolean).join(path.delimiter);
  const cliArgs = [
    "-m", "teeni_analysis_core.cli", "report-model",
    "--primary", primary,
    "--primary-scene", String(values["--primary-scene"]),
    "--primary-label", primaryLabel,
    "--primary-detail", primaryDetailPath,
    "--primary-ending-detail", endingDetailPath,
    "--rules", effectiveRulesPath,
    "--output", reportPath,
  ];
  const initialSourceHashes = new Map();
  for (const source of [primary, baseline].filter(Boolean)) initialSourceHashes.set(source, await sha256(source));
  if (baseline) {
    cliArgs.push(
      "--baseline", baseline,
      "--baseline-scene", String(values["--baseline-scene"]),
      "--baseline-label", values["--baseline-label"] ?? "基准版",
      "--baseline-detail", baselineDetailPath,
    );
  }
  run(python, cliArgs, { env: { ...process.env, PYTHONPATH: pythonPath, PYTHONUTF8: "1" } });
  run(python, [
    path.join(scriptDir, "safety-review.py"), "build",
    "--report", reportPath, "--detail", primaryDetailPath,
    "--data-date", values["--data-date"], "--output", safetyWorkbookPath,
    "--exclusions", safetyExclusionsPath,
  ], { env: { ...process.env, PYTHONPATH: pythonPath, PYTHONUTF8: "1" } });

  const nodeModules = process.env.TEENI_NODE_MODULES ?? path.resolve(scriptDir, "../node_modules");
  run(process.execPath, [
    path.join(scriptDir, "render-shared.mjs"),
    "--report", reportPath,
    "--output", outputPath,
  ], { env: {
    ...process.env,
    TEENI_NODE_MODULES: nodeModules,
    NODE_OPTIONS: renderNodeOptions(process.env.NODE_OPTIONS),
  } });

  const report = JSON.parse(await fs.readFile(reportPath, "utf8"));
  for (const [source, hash] of initialSourceHashes) {
    if (await sha256(source) !== hash) throw new Error(`Source changed during analysis: ${path.basename(source)}`);
  }
  const sources = [{
    role: "primary",
    file: path.basename(primary),
    sha256: await sha256(primary),
    rows: report.primary.summary.rows,
    sceneId: String(values["--primary-scene"]),
  }];
  if (baseline) {
    sources.push({
      role: "baseline",
      file: path.basename(baseline),
      sha256: await sha256(baseline),
      rows: report.baseline.summary.rows,
      sceneId: String(values["--baseline-scene"]),
    });
  }
  const outputs = [
    await outputRecord("workbook", outputPath),
    await outputRecord("primary_detail", primaryDetailPath, report.primary.summary.rows),
    await outputRecord("ending_detail", endingDetailPath, report.primary.endingAnalysis.cohortSessions),
    await outputRecord("effective_rules", effectiveRulesPath),
    await outputRecord("safety_workbook", safetyWorkbookPath),
    await outputRecord("safety_exclusions", safetyExclusionsPath, report.primary.safetyGameExclusions.length),
  ];
  await fs.writeFile(supplementalMetricsPath, `${JSON.stringify({
    schemaVersion: "teeni-base-supplemental-metrics/1.0.0",
    coreVersion: report.coreVersion,
    rulesVersion: report.rulesVersion,
    sceneId: String(values["--primary-scene"]),
    primary: report.primary.supplementalMetrics,
    ...(report.baseline ? { baseline: report.baseline.supplementalMetrics } : {}),
  }, null, 2)}\n`, "utf8");
  outputs.push(await outputRecord("supplemental_metrics", supplementalMetricsPath));
  await fs.writeFile(sessionStructurePath, `${JSON.stringify({
    schemaVersion: "teeni-session-structure/1.0.0",
    coreVersion: report.coreVersion, rulesVersion: report.rulesVersion,
    sceneId: String(values["--primary-scene"]), dataDate: values["--data-date"],
    primary: report.primary.sessionStructure,
    ...(report.baseline ? { baseline: report.baseline.sessionStructure } : {}),
  }, null, 2)}\n`, "utf8");
  outputs.push(await outputRecord("session_structure", sessionStructurePath));
  if (baselineDetailPath) {
    outputs.push(await outputRecord("baseline_detail", baselineDetailPath, report.baseline.summary.rows));
  }
  const manifest = {
    schemaVersion: "teeni-base-bundle-manifest/1.0.0",
    contractVersion: "teeni-base-detail/1.2.0",
    coreVersion: report.coreVersion,
    rulesVersion: report.rulesVersion,
    mode: report.mode,
    sceneId: String(values["--primary-scene"]),
    createdAt: new Date().toISOString(),
    dataDate: values["--data-date"],
    sources,
    outputs,
  };
  await fs.writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  completed = true;
} catch (error) {
  primaryError = error;
}

await finalizeRun({
  tempDir,
  outputPaths: [outputPath, primaryDetailPath, baselineDetailPath, endingDetailPath, manifestPath, effectiveRulesPath, supplementalMetricsPath, sessionStructurePath, safetyWorkbookPath, safetyExclusionsPath].filter(Boolean),
  completed,
  primaryError,
});

console.log(JSON.stringify({
  outputPath,
  primaryDetailPath,
  baselineDetailPath,
  endingDetailPath,
  manifestPath,
  effectiveRulesPath,
  supplementalMetricsPath,
  sessionStructurePath,
  safetyWorkbookPath,
  safetyExclusionsPath,
  dataDate: values["--data-date"],
  primarySourcePath: primary,
  baselineSourcePath: baseline,
  elapsedMs: Date.now() - startedAt,
  engine: "teeni-analysis-core",
  coreVersion: "2.7.0",
  contractVersion: "teeni-base-detail/1.2.0",
}));

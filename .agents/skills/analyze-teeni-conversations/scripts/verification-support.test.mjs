import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { sha256, verifyManifest, validateFixedContract } from "./verification-support.mjs";

const temp = await fs.mkdtemp(path.join(os.tmpdir(), "teeni-manifest-test-"));
try {
  const outputs = new Map();
  const sources = new Map();
  for (const role of ["workbook", "primary_detail", "ending_detail", "effective_rules", "primary"]) {
    const file = path.join(temp, `${role}.txt`);
    await fs.writeFile(file, role);
    const record = { path: file, ...(["primary_detail", "ending_detail", "primary"].includes(role) ? { rows: 1 } : {}), ...(role === "primary" ? { sceneId: "488" } : {}) };
    (role === "primary" ? sources : outputs).set(role, record);
  }
  const entries = async (items) => {
    const result = [];
    for (const [role, item] of items) result.push({ role, file: path.basename(item.path), sha256: await sha256(item.path), ...(item.rows === undefined ? {} : { rows: item.rows }), ...(item.sceneId ? { sceneId: item.sceneId } : {}) });
    return result;
  };
  const manifest = { schemaVersion: "teeni-base-bundle-manifest/1.0.0", contractVersion: "teeni-base-detail/1.2.0", coreVersion: "2.6.0", rulesVersion: "14.0.0", sceneId: "488", mode: "primary", outputs: await entries(outputs), sources: await entries(sources) };
  const options = { sceneId: "488", rulesVersion: "14.0.0", mode: "primary", outputs, sources };
  await verifyManifest(manifest, options);
  await verifyManifest({ ...manifest, coreVersion: "2.5.1", rulesVersion: "13.0.0" }, { ...options, rulesVersion: "13.0.0" });
  const invalid = [
    (value) => { value.outputs = []; },
    (value) => { value.outputs[1] = value.outputs[0]; },
    (value) => { value.sources = []; },
    (value) => { value.sceneId = "904"; },
    (value) => { value.sources[0].sceneId = "904"; },
    (value) => { value.sources[0].sha256 = "0".repeat(64); },
    (value) => { value.sources[0].rows = 2; },
    (value) => { value.outputs[1].rows = 2; },
    (value) => { value.outputs[0].file = "../workbook.txt"; },
    (value) => { value.outputs[0].file = "D:\\workbook.txt"; },
  ];
  for (const mutate of invalid) {
    const value = structuredClone(manifest);
    mutate(value);
    await assert.rejects(verifyManifest(value, options));
  }
  const rules = JSON.parse(await fs.readFile(new URL("../references/default-rules.json", import.meta.url), "utf8"));
  validateFixedContract(rules);
  const changed = structuredClone(rules);
  changed.ending_analysis.fallback_reopen.window_seconds = 600;
  assert.throws(() => validateFixedContract(changed), /cannot be overridden/);
  const python = process.env.TEENI_ANALYSIS_PYTHON;
  assert(python, "TEENI_ANALYSIS_PYTHON is required");
  const test = spawnSync(python, [fileURLToPath(new URL("verify-package.test.py", import.meta.url))], { encoding: "utf8", env: { ...process.env, PYTHONUTF8: "1" } });
  assert.equal(test.status, 0, test.stderr);
  console.log(JSON.stringify({ status: "ok", manifestCases: invalid.length + 1, python: test.stderr.trim() }));
} finally {
  await fs.rm(temp, { recursive: true, force: true });
}

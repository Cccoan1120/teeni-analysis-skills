import crypto from "node:crypto";
import { createReadStream } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import assert from "node:assert/strict";

export function deepMerge(base, override) {
  const output = structuredClone(base);
  for (const [key, value] of Object.entries(override)) {
    output[key] = value && typeof value === "object" && !Array.isArray(value)
      && output[key] && typeof output[key] === "object" && !Array.isArray(output[key])
      ? deepMerge(output[key], value) : structuredClone(value);
  }
  return output;
}

export async function readJsonArgument(value) {
  if (!value) return {};
  let text = value;
  try {
    if ((await fs.stat(path.resolve(value))).isFile()) text = await fs.readFile(path.resolve(value), "utf8");
  } catch (error) {
    if (!["ENOENT", "EINVAL", "ENAMETOOLONG"].includes(error.code)) throw error;
  }
  const parsed = JSON.parse(text.replace(/^\uFEFF/, ""));
  assert(parsed && typeof parsed === "object" && !Array.isArray(parsed), "rules must be a JSON object");
  return parsed;
}

export function validateFixedContract(rules) {
  const fallback = rules.ending_analysis.fallback_reopen;
  assert.equal(rules.ending_analysis.minimum_effective_turns, 5, "five-turn cohort cannot be overridden");
  assert.equal(fallback.window_seconds, 300, "five-minute fallback window cannot be overridden");
  assert.deepEqual([...fallback.trigger_queries].sort(), ["对", "嗯"].sort(), "fallback trigger queries cannot be overridden");
  assert(Array.isArray(fallback.fixed_replies) && fallback.fixed_replies.length > 0, "fallback replies must not be empty");
  for (const item of fallback.fixed_replies) {
    assert(typeof item.text === "string" && item.text.trim() && typeof item.type === "string" && item.type.trim(), "fallback replies require text and type");
  }
}

export async function sha256(filePath) {
  const digest = crypto.createHash("sha256");
  for await (const chunk of createReadStream(filePath)) digest.update(chunk);
  return digest.digest("hex");
}

function safeFile(file) {
  return typeof file === "string" && file.length > 0 && !/[\\/:]/.test(file) && ![".", ".."].includes(file);
}

export async function verifyManifest(manifest, { sceneId, rulesVersion, mode, outputs, sources }) {
  assert.equal(manifest.schemaVersion, "teeni-base-bundle-manifest/1.0.0", "manifest schema mismatch");
  assert.equal(manifest.contractVersion, "teeni-base-detail/1.2.0", "detail contract mismatch");
  assert.equal(manifest.coreVersion, { "13.0.0": "2.5.1", "14.0.0": "2.6.0", "15.0.0": "2.6.1", "16.0.0": "2.7.0" }[rulesVersion], "core version mismatch");
  assert.equal(manifest.rulesVersion, rulesVersion, "rules version mismatch");
  assert.equal(manifest.sceneId, sceneId, "manifest scene mismatch");
  assert.equal(manifest.mode, mode, "manifest mode mismatch");
  for (const [group, expected] of [["outputs", outputs], ["sources", sources]]) {
    const entries = manifest[group];
    assert(Array.isArray(entries), `manifest ${group} missing`);
    assert.equal(entries.length, expected.size, `manifest ${group} role count mismatch`);
    const seen = new Set();
    for (const item of entries) {
      assert(item && expected.has(item.role) && !seen.has(item.role), `unexpected or duplicate ${group} role: ${item?.role}`);
      seen.add(item.role);
      const actual = expected.get(item.role);
      assert(safeFile(item.file), `unsafe manifest filename for ${item.role}`);
      assert.equal(item.file, path.basename(actual.path), `manifest filename mismatch for ${item.role}`);
      assert(/^[a-f0-9]{64}$/.test(item.sha256), `invalid SHA-256 for ${item.role}`);
      assert.equal(item.sha256, await sha256(actual.path), `manifest hash mismatch for ${item.role}`);
      if (actual.rows !== undefined) assert.equal(item.rows, actual.rows, `manifest row count mismatch for ${item.role}`);
      if (actual.sceneId !== undefined) assert.equal(item.sceneId, actual.sceneId, `manifest source scene mismatch for ${item.role}`);
    }
  }
}

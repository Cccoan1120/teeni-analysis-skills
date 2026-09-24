import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { fileURLToPath } from "node:url";
import os from "node:os";
import { spawnSync } from "node:child_process";

const args = {};
for (let index = 2; index < process.argv.length; index += 2) args[process.argv[index]] = process.argv[index + 1];
if (!args["--workbook"] || !args["--output-dir"]) throw new Error("--workbook and --output-dir are required");
const require = createRequire(import.meta.url);
const dependency = require.resolve("@oai/artifact-tool", { paths: [process.env.TEENI_NODE_MODULES] });
const { SpreadsheetFile } = await import(pathToFileURL(dependency).href);
const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "teeni-safety-preview-"));
const preview = path.join(temporary, "preview.xlsx");
const extracted = spawnSync(process.env.TEENI_ANALYSIS_PYTHON || "python", [
  path.join(path.dirname(fileURLToPath(import.meta.url)), "safety-preview.py"), args["--workbook"], preview,
], { encoding: "utf8" });
if (extracted.status !== 0) {
  await fs.rm(temporary, { recursive: true, force: true });
  throw new Error(`Safety preview extraction failed: ${extracted.stderr}`);
}
let workbook;
try {
  workbook = await SpreadsheetFile.importXlsx(new Uint8Array(await fs.readFile(preview)));
} finally {
  await fs.rm(temporary, { recursive: true, force: true });
}
const columns = new Map([["复核总览", "F"], ["AI回复候选", "R"], ["用户风险表达", "R"], ["需关注用户", "O"], ["会话上下文", "L"], ["游戏排除记录", "P"]]);
await fs.mkdir(args["--output-dir"], { recursive: true });
for (const [name, last] of columns) {
  const rendered = await workbook.render({ sheetName: name, range: `A1:${last}${name === "复核总览" ? 26 : 5}`, scale: 1, format: "png" });
  const bytes = new Uint8Array(await rendered.arrayBuffer());
  if (bytes.length < 500) throw new Error(`Blank safety preview: ${name}`);
  await fs.writeFile(path.join(args["--output-dir"], `${name}.png`), bytes);
}
console.log(JSON.stringify({ renderedSheets: columns.size, outputDir: args["--output-dir"] }));

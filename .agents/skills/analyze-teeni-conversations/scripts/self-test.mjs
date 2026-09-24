import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const analyzer = path.join(scriptDir, "analyze-shared.mjs");
const verifier = path.join(scriptDir, "verify.mjs");
const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "teeni-base-self-test-"));
const boundaryDelays = [60, 120, 180, 300, 600, 1800, 3600, 10800, 21600, 86400, 86401];
const constellationBoundaries = [
  ["摩羯座", "2020-12-22"], ["摩羯座", "2020-01-19"],
  ["水瓶座", "2020-01-20"], ["水瓶座", "2020-02-18"],
  ["双鱼座", "2020-02-19"], ["双鱼座", "2020-03-20"],
  ["白羊座", "2020-03-21"], ["白羊座", "2020-04-19"],
  ["金牛座", "2020-04-20"], ["金牛座", "2020-05-20"],
  ["双子座", "2020-05-21"], ["双子座", "2020-06-21"],
  ["巨蟹座", "2020-06-22"], ["巨蟹座", "2020-07-22"],
  ["狮子座", "2020-07-23"], ["狮子座", "2020-08-22"],
  ["处女座", "2020-08-23"], ["处女座", "2020-09-22"],
  ["天秤座", "2020-09-23"], ["天秤座", "2020-10-23"],
  ["天蝎座", "2020-10-24"], ["天蝎座", "2020-11-22"],
  ["射手座", "2020-11-23"], ["射手座", "2020-12-21"],
];
const profileFixtureRows = constellationBoundaries.length + 8;
delete process.env.DASHSCOPE_API_KEY;
delete process.env.DASHSCOPE_BASE_URL;

function csvCell(value) {
  const text = String(value ?? "");
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function fixture(sceneId, ordinary = false, includeRiskRows = true) {
  const headers = ["id", "clientId", "cid", "text", "response", "timestamp", "intention", "subIntention", "created_at", "sceneId", "model"];
  const rows = [];
  for (let turn = 1; turn <= 6; turn += 1) {
    rows.push({
      id: `a-${sceneId}-${turn}`, clientId: "anonymous-a", cid: `cid-${sceneId}-a`,
      text: turn === 6 ? "嗯" : (turn === 5 ? "What does #N/A mean?" : `匿名有效问题${turn}`),
      response: JSON.stringify({ generated_text: `匿名回复${turn}`, extra: { intent_name: "知识探索｜百科提问", age: 9, gender: "女" } }),
      timestamp: turn, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
      created_at: `2026-01-01 00:00:0${turn}`, sceneId, model: "anonymous",
    });
  }
  rows.push({
    id: `b-${sceneId}-1`, clientId: "anonymous-b", cid: `cid-${sceneId}-b`, text: "##{startPrompt}##",
    response: JSON.stringify({ generated_text: "你好", extra: { intent_name: "闲聊｜问候", age: 7, gender: "男" } }),
    timestamp: 20, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
    created_at: "2026-01-01 00:00:20", sceneId, model: "anonymous",
  });
  rows.push({
    id: `c-${sceneId}-1`, clientId: "anonymous-c", cid: `cid-${sceneId}-c`, text: "嗯",
    response: JSON.stringify({ generated_text: "匿名回复", extra: { intent_name: "格式错误", age: 8, gender: "女" } }),
    timestamp: 21, intention: ordinary ? "普通主意图" : "不应回退", subIntention: ordinary ? "普通子意图" : "不应回退",
    created_at: "2026-01-01 00:00:21", sceneId, model: "anonymous",
  });
  for (let turn = 1; turn <= 5; turn += 1) {
    rows.push({
      id: `d-${sceneId}-${turn}`, clientId: "anonymous-d", cid: `cid-${sceneId}-d`, text: turn === 1 ? "匿名有效问题" : "对",
      response: JSON.stringify({ generated_text: turn === 1 ? "匿名有效回复" : "小朋友，请靠近我，按住按键对话哦。", extra: { intent_name: "日常社交｜简短确认", age: 10, gender: "男" } }),
      timestamp: 30 + turn, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
      created_at: `2026-01-01 00:00:${30 + turn}`, sceneId, model: "anonymous",
    });
  }
  rows.push({
    id: `d-${sceneId}-next`, clientId: "anonymous-d", cid: `cid-${sceneId}-d-next`, text: "继续聊天",
    response: JSON.stringify({ generated_text: "我们继续。", extra: { intent_name: "日常社交｜继续聊天", age: 10, gender: "男" } }),
    timestamp: 100, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
    created_at: "2026-01-01 00:01:40", sceneId, model: "anonymous",
  });
  for (let turn = 1; turn <= 5; turn += 1) {
    rows.push({
      id: `e-${sceneId}-${turn}`, clientId: "anonymous-e", cid: `cid-${sceneId}-e`, text: turn === 5 ? "嗯。" : `匿名有效问题e${turn}`,
      response: JSON.stringify({ generated_text: turn === 5 ? "请靠近我，不要堵住麦克风，再说一遍哦。" : `匿名回复e${turn}`, extra: { intent_name: "日常社交｜简短确认", age: 8, gender: "女" } }),
      timestamp: 195 + turn, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
      created_at: `2026-01-01 00:03:${15 + turn}`, sceneId, model: "anonymous",
    });
  }
  rows.push({
    id: `e-${sceneId}-next`, clientId: "anonymous-e", cid: `cid-${sceneId}-e-next`, text: "继续聊天",
    response: JSON.stringify({ generated_text: "我们继续。", extra: { intent_name: "日常社交｜继续聊天", age: 8, gender: "女" } }),
    timestamp: 501, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
    created_at: "2026-01-01 00:08:21", sceneId, model: "anonymous",
  });
  for (let turn = 1; turn <= 5; turn += 1) {
    rows.push({
      id: `f-${sceneId}-${turn}`, clientId: "anonymous-f", cid: `cid-${sceneId}-f`, text: turn === 5 ? "对" : `匿名有效问题f${turn}`,
      response: JSON.stringify({ generated_text: turn === 5 ? "这是普通回复，不是固定兜底。" : `匿名回复f${turn}`, extra: { intent_name: "日常社交｜简短确认", age: 7, gender: "男" } }),
      timestamp: 600 + turn, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
      created_at: `2026-01-01 00:10:0${turn}`, sceneId, model: "anonymous",
    });
  }
  for (let turn = 1; turn <= 5; turn += 1) {
    rows.push({
      id: `g-${sceneId}-${turn}`, clientId: "anonymous-g", cid: `cid-${sceneId}-g`, text: turn === 5 ? "嗯" : `匿名有效问题g${turn}`,
      response: JSON.stringify({ generated_text: turn === 5 ? "小朋友，请靠近我，按住按键对话哦。" : `匿名回复g${turn}`, extra: { intent_name: "日常社交｜简短确认", age: 9, gender: "女" } }),
      timestamp: 700 + turn, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
      created_at: `2026-01-01 00:11:4${turn}`, sceneId, model: "anonymous",
    });
  }
  rows.push({
    id: `h-${sceneId}-1`, clientId: "anonymous-h", cid: `cid-${sceneId}-h`, text: "其他用户的新会话",
    response: JSON.stringify({ generated_text: "不会连接到 anonymous-g。", extra: { intent_name: "日常社交｜继续聊天", age: 9, gender: "女" } }),
    timestamp: 715, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
    created_at: "2026-01-01 00:11:55", sceneId, model: "anonymous",
  });
  let latestTimestamp = 0;
  boundaryDelays.forEach((delay, caseIndex) => {
    const clientId = `anonymous-boundary-${caseIndex}`;
    const cid = `cid-${sceneId}-boundary-${caseIndex}`;
    const query = caseIndex % 2 === 0 ? "对" : "嗯";
    const finalTimestamp = 100000 + caseIndex * 1000;
    for (let turn = 1; turn <= 5; turn += 1) {
      rows.push({
        id: `boundary-${sceneId}-${caseIndex}-${turn}`, clientId, cid,
        text: turn === 5 ? query : `匿名边界问题${caseIndex}-${turn}`,
        response: JSON.stringify({ generated_text: turn === 5 ? "小朋友，请靠近我，按住按键对话哦。" : `匿名边界回复${caseIndex}-${turn}`, extra: { intent_name: "日常社交｜简短确认", age: 9, gender: caseIndex % 2 ? "女" : "男" } }),
        timestamp: finalTimestamp - 5 + turn, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
        created_at: `2026-01-02 00:${String(caseIndex).padStart(2, "0")}:${String(turn).padStart(2, "0")}`, sceneId, model: "anonymous",
      });
    }
    const nextTimestamp = finalTimestamp + delay;
    latestTimestamp = Math.max(latestTimestamp, nextTimestamp);
    rows.push({
      id: `boundary-${sceneId}-${caseIndex}-next`, clientId, cid: `${cid}-next`, text: "继续聊天",
      response: JSON.stringify({ generated_text: "继续。", extra: { intent_name: "日常社交｜继续聊天", age: 9, gender: caseIndex % 2 ? "女" : "男" } }),
      timestamp: nextTimestamp, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
      created_at: `2026-01-03 00:${String(caseIndex).padStart(2, "0")}:00`, sceneId, model: "anonymous",
    });
  });
  for (let turn = 1; turn <= 5; turn += 1) {
    rows.push({
      id: `observable-no-next-${sceneId}-${turn}`, clientId: "anonymous-observable-no-next", cid: `cid-${sceneId}-observable-no-next`,
      text: turn === 5 ? "对" : `匿名完整观察问题${turn}`,
      response: JSON.stringify({ generated_text: turn === 5 ? "小朋友，请靠近我，按住按键对话哦。" : `匿名完整观察回复${turn}`, extra: { intent_name: "日常社交｜简短确认", age: 9, gender: "男" } }),
      timestamp: 1000 - 5 + turn, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
      created_at: `2026-01-01 01:00:0${turn}`, sceneId, model: "anonymous",
    });
  }
  const noNextTimestamp = latestTimestamp + 10;
  for (let turn = 1; turn <= 5; turn += 1) {
    rows.push({
      id: `censored-${sceneId}-${turn}`, clientId: "anonymous-censored", cid: `cid-${sceneId}-censored`,
      text: turn === 5 ? "嗯" : `匿名截断问题${turn}`,
      response: JSON.stringify({ generated_text: turn === 5 ? "请靠近我，不要堵住麦克风，再说一遍哦。" : `匿名截断回复${turn}`, extra: { intent_name: "日常社交｜简短确认", age: 8, gender: "女" } }),
      timestamp: noNextTimestamp - 5 + turn, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
      created_at: `2026-01-04 00:00:0${turn}`, sceneId, model: "anonymous",
    });
  }
  rows.push({
    id: `cutoff-${sceneId}`, clientId: "anonymous-cutoff", cid: `cid-${sceneId}-cutoff`, text: "导出截止标记",
    response: JSON.stringify({ generated_text: "匿名回复", extra: { intent_name: "日常社交｜继续聊天", age: 8, gender: "女" } }),
    timestamp: noNextTimestamp + 30, intention: ordinary ? "普通主意图" : "错误回退值", subIntention: ordinary ? "普通子意图" : "错误回退值",
    created_at: "2026-01-04 00:00:40", sceneId, model: "anonymous",
  });
  const riskRows = [
    ["p0", "anonymous-risk-p0", "p0", "我不想活", JSON.stringify({ generated_text: "不要伤害自己，请马上告诉妈妈。", extra: { intent_name: "情绪支持｜求助", age: 9, gender: "女" } })],
    ["parse", "anonymous-risk-parse", "parse", "我想赌博", "{not-json"],
    ["empty", "anonymous-risk-empty", "empty", "我想玩火", JSON.stringify({ generated_text: "", extra: { intent_name: "日常社交｜其他", age: 8, gender: "男" } })],
    ["ai-only", "anonymous-ai-only", "ai-only", "讲一个普通故事", JSON.stringify({ generated_text: "你可以玩火。", extra: { intent_name: "故事｜普通", age: 8, gender: "男" } })],
    ["multi", "anonymous-risk-multi", "multi", "我想赌博还想玩火", JSON.stringify({ generated_text: "我们聊安全的话题。", extra: { intent_name: "日常社交｜其他", age: 10, gender: "女" } })],
    ["repeat-1", "anonymous-risk-repeat", "repeat-a", "故事里有人开枪", JSON.stringify({ generated_text: "这是虚构情节。", extra: { intent_name: "故事｜普通", age: 11, gender: "男" } })],
    ["repeat-2", "anonymous-risk-repeat", "repeat-b", "故事里有人开枪", JSON.stringify({ generated_text: "这是虚构情节。", extra: { intent_name: "故事｜普通", age: 11, gender: "男" } })],
    ["repeat-3", "anonymous-risk-repeat", "repeat-b", "又提到开枪", JSON.stringify({ generated_text: "这是虚构情节。", extra: { intent_name: "故事｜普通", age: 11, gender: "男" } })],
    ["p2", "anonymous-risk-p2", "p2", "坏人想开枪", JSON.stringify({ generated_text: "不要模仿。", extra: { intent_name: "故事｜普通", age: 7, gender: "女" } })],
    ["context", "anonymous-risk-context", "context", "故事里写着拿刀试试", JSON.stringify({ generated_text: "这需要人工结合语境复核。", extra: { intent_name: "故事｜普通", age: 12, gender: "女" } })],
    ["template", "anonymous-risk-template", "template", "和我打招呼并称呼我的名字，围绕一个##炸弹##话题挑选一个真实的历史知识或者百科知识带上强相关问题思考引导我聊天！", JSON.stringify({ generated_text: "你好。", extra: { intent_name: "开场｜模板", age: 8, gender: "男" } })],
  ];
  if (includeRiskRows) {
    riskRows.forEach(([suffix, clientId, cidSuffix, text, response], index) => rows.push({
      id: `risk-${sceneId}-${suffix}`, clientId, cid: `cid-${sceneId}-risk-${cidSuffix}`, text, response,
      timestamp: 300000 + index, intention: ordinary ? "普通主意图" : "错误回退值",
      subIntention: ordinary ? "普通子意图" : "错误回退值",
      created_at: `2026-01-05 00:00:${String(index).padStart(2, "0")}`, sceneId, model: "anonymous",
    }));
  }
  constellationBoundaries.forEach(([expectedConstellation, birthday], index) => rows.push({
    id: `profile-${sceneId}-boundary-${index}`, clientId: `anonymous-profile-boundary-${index}`,
    cid: `cid-${sceneId}-profile-boundary-${index}`, text: `匿名星座边界${index}`,
    response: JSON.stringify({ generated_text: "匿名回复", extra: {
      intent_name: "知识探索｜百科提问", age: 9, gender: "女", birthday,
      city: index === 0 ? "  北京市  " : `匿名城市${index}市`, expectedConstellation,
    } }),
    timestamp: 400000 + index, intention: ordinary ? "普通主意图" : "错误回退值",
    subIntention: ordinary ? "普通子意图" : "错误回退值",
    created_at: `2026-01-06 00:00:${String(index).padStart(2, "0")}`, sceneId, model: "anonymous",
  }));
  const profileCases = [
    ["leap", "anonymous-profile-leap", { birthday: "2020-02-29", city: "阿克苏地区" }],
    ["invalid", "anonymous-profile-invalid", { birthday: "2021-02-29", city: { name: "无效城市" } }],
    ["missing", "anonymous-profile-missing", {}],
    ["mixed-1", "anonymous-profile-mixed", { birthday: "2020-05-28", city: "深圳市" }],
    ["mixed-2", "anonymous-profile-mixed", { birthday: "2020/05/28", city: { name: "深圳" } }],
    ["conflict-1", "anonymous-profile-conflict", { birthday: "2020-05-28", city: "杭州市" }],
    ["conflict-2", "anonymous-profile-conflict", { birthday: "2020-08-28", city: "宁波市" }],
    ["nfkc", "anonymous-profile-nfkc", { birthday: "2020-05-28", city: "ＡＢＣ市" }],
  ];
  profileCases.forEach(([suffix, clientId, profile], index) => rows.push({
    id: `profile-${sceneId}-${suffix}`, clientId, cid: `cid-${sceneId}-profile-${suffix}`,
    text: `匿名画像测试${suffix}`,
    response: JSON.stringify({ generated_text: "匿名回复", extra: {
      intent_name: "知识探索｜百科提问", age: 9, gender: "女", ...profile,
    } }),
    timestamp: 401000 + index, intention: ordinary ? "普通主意图" : "错误回退值",
    subIntention: ordinary ? "普通子意图" : "错误回退值",
    created_at: `2026-01-06 00:01:${String(index).padStart(2, "0")}`, sceneId, model: "anonymous",
  }));
  const lines = [headers, ...rows.map((row) => headers.map((header) => row[header]))];
  return lines.map((row) => row.map(csvCell).join(",")).join("\r\n");
}

function parseCsv(text) {
  const output = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') { field += '"'; index += 1; }
      else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"' && field === "") quoted = true;
    else if (char === ",") { row.push(field); field = ""; }
    else if (char === "\r" || char === "\n") {
      if (char === "\r" && text[index + 1] === "\n") index += 1;
      row.push(field); output.push(row); row = []; field = "";
    } else field += char;
  }
  if (field || row.length) { row.push(field); output.push(row); }
  return output;
}

async function verifyProfileCases(detailPath) {
  const rows = parseCsv((await fs.readFile(detailPath, "utf8")).replace(/^\uFEFF/, ""));
  const headers = rows.shift();
  const records = rows.map((values) => Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""])));
  const byClient = new Map();
  for (const record of records) {
    if (!byClient.has(record.clientId)) byClient.set(record.clientId, []);
    byClient.get(record.clientId).push(record);
  }
  constellationBoundaries.forEach(([expected], index) => {
    const record = byClient.get(`anonymous-profile-boundary-${index}`)?.[0];
    if (record?.birthday_status !== "正常" || record.constellation !== expected) throw new Error(`constellation boundary ${index} mismatch`);
  });
  const leap = byClient.get("anonymous-profile-leap")?.[0];
  if (leap?.constellation !== "双鱼座" || leap.city_normalized !== "阿克苏地区") throw new Error("leap-day or region normalization mismatch");
  const invalid = byClient.get("anonymous-profile-invalid")?.[0];
  if (invalid?.birthday_status !== "非法" || invalid.city_status !== "非法") throw new Error("invalid profile status mismatch");
  const missing = byClient.get("anonymous-profile-missing")?.[0];
  if (missing?.birthday_status !== "缺失" || missing.city_status !== "缺失") throw new Error("missing profile status mismatch");
  const mixed = byClient.get("anonymous-profile-mixed") ?? [];
  if (!mixed.length || mixed.some((row) => row.birthday_status !== "含非法值" || row.city_status !== "含非法值")) throw new Error("mixed-invalid profile status mismatch");
  const conflict = byClient.get("anonymous-profile-conflict") ?? [];
  if (!conflict.length || conflict.some((row) => row.birthday_status !== "冲突" || row.city_status !== "冲突")) throw new Error("conflicting profile status mismatch");
  if (byClient.get("anonymous-profile-boundary-0")?.[0]?.city_normalized !== "北京") throw new Error("Beijing normalization mismatch");
  if (byClient.get("anonymous-profile-nfkc")?.[0]?.city_normalized !== "ABC") throw new Error("city NFKC mismatch");
}

function run(script, args) {
  const result = spawnSync(process.execPath, [script, ...args], {
    encoding: "utf8", maxBuffer: 128 * 1024 * 1024, env: { ...process.env },
  });
  if (result.status !== 0) throw new Error(`${path.basename(script)} failed\n${result.stdout}\n${result.stderr}`);
  return result.stdout;
}

function resultJson(stdout, key) {
  for (const line of stdout.trim().split(/\r?\n/).reverse()) {
    try { const value = JSON.parse(line); if (key in value) return value; } catch { /* diagnostics */ }
  }
  throw new Error(`missing JSON result with ${key}`);
}

async function analyzeCase(name, sceneId, options = {}) {
  const source = path.join(tempRoot, `${name}.csv`);
  let sourceText = fixture(sceneId, options.ordinary, options.includeRiskRows !== false);
  if (options.customFallback) sourceText = sourceText.replaceAll("小朋友，请靠近我，按住按键对话哦。", "匿名实验兜底回复。");
  await fs.writeFile(source, sourceText, "utf8");
  const args = ["--primary", source, "--primary-scene", sceneId, "--data-date", "2026-09-08", "--output-dir", path.join(tempRoot, name)];
  if (options.customFallback) args.push("--overrides", JSON.stringify({ ending_analysis: { fallback_reopen: { fixed_replies: [
    { text: "匿名实验兜底回复。", type: "实验兜底" },
    { text: "请靠近我，不要堵住麦克风，再说一遍哦。", type: "麦克风遮挡提示" },
  ] } } }));
  if (options.baseline) {
    const baseline = path.join(tempRoot, `${name}-baseline.csv`);
    await fs.writeFile(baseline, fixture(options.baseline.sceneId, options.baseline.ordinary), "utf8");
    args.push("--baseline", baseline, "--baseline-scene", options.baseline.sceneId);
  }
  const output = resultJson(run(analyzer, args), "manifestPath");
  const verifyArgs = [
    "--workbook", output.outputPath,
    "--primary-detail", output.primaryDetailPath,
    "--ending-detail", output.endingDetailPath,
    "--manifest", output.manifestPath,
    "--primary-scene", sceneId,
    "--primary-source", source,
    "--expected-primary-rows", String(31 + boundaryDelays.length * 6 + 11 + profileFixtureRows + (options.includeRiskRows === false ? 0 : 11)),
    "--expected-ending-rows", "1",
    "--render-dir", path.join(tempRoot, `${name}-renders`),
  ];
  if (output.baselineDetailPath) verifyArgs.push("--baseline-detail", output.baselineDetailPath, "--baseline-source", output.baselineSourcePath);
  const verified = resultJson(run(verifier, verifyArgs), "manifestVerified");
  if (["488", "901", "904"].includes(sceneId)) await verifyProfileCases(output.primaryDetailPath);
  if (verified.rawEndingRows !== 5 + boundaryDelays.length + 2) throw new Error(`${name} raw ending cohort mismatch`);
  if (verified.fallbackTriggers !== 3 + boundaryDelays.length + 2 || verified.fallbackReopened !== 5) throw new Error(`${name} fallback reopen mismatch`);
  if (verified.fallbackTriggerUsers !== 3 + boundaryDelays.length + 2 || verified.fallbackReopenedUsers !== 5) throw new Error(`${name} fallback user mismatch`);
  const expectedRisk = options.includeRiskRows === false
    ? { expressions: 0, ruleHits: 0, users: 0, p0: 0, p1: 0, p2: 0, aiSafety: 0 }
    : { expressions: 9, ruleHits: 10, users: 7, p0: 1, p1: 2, p2: 4, aiSafety: 1 };
  if (verified.userRiskExpressions !== expectedRisk.expressions || verified.userRiskRuleHits !== expectedRisk.ruleHits) throw new Error(`${name} user-risk expression mismatch`);
  if (verified.attentionUsers !== expectedRisk.users || verified.attentionP0 !== expectedRisk.p0 || verified.attentionP1 !== expectedRisk.p1 || verified.attentionP2 !== expectedRisk.p2) throw new Error(`${name} attention priority mismatch`);
  if (verified.aiSafetyCandidates !== expectedRisk.aiSafety) throw new Error(`${name} AI safety regression mismatch`);
  const expectedSheets = (["488", "901", "904"].includes(sceneId) ? 23 : 20) + (options.baseline ? 1 : 0);
  if (verified.sheets !== expectedSheets) throw new Error(`${name} sheet-count mismatch`);
  return { name, ...verified };
}

const results = [];
run(path.join(scriptDir, "verification-support.test.mjs"), []);
results.push(await analyzeCase("scene-488", "488"));
results.push(await analyzeCase("scene-901", "901"));
results.push(await analyzeCase("scene-904", "904"));
results.push(await analyzeCase("ordinary-comparison", "777", {
  ordinary: true,
  baseline: { sceneId: "778", ordinary: true },
}));
results.push(await analyzeCase("empty-detail-tables", "777", {
  ordinary: true,
  includeRiskRows: false,
}));
results.push(await analyzeCase("override-fallback", "488", { customFallback: true }));

const checks = results.reduce((total, item) => total + item.sheets + item.primaryRows + item.endingRows, 0);
console.log(JSON.stringify({
  status: "ok",
  anonymous: true,
  dashscopeEnvironmentRequired: false,
  cases: results,
  checks,
  tempRoot,
}));

import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const COLORS = {
  navy: "#173F5F",
  teal: "#0F766E",
  tealSoft: "#E4F3EF",
  blueSoft: "#E7F0F8",
  amber: "#B45309",
  amberSoft: "#FFF2D8",
  ink: "#263746",
  muted: "#5B6B79",
  line: "#D8E1E8",
  pale: "#F3F6F8",
  white: "#FFFFFF",
};

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) values[argv[index]] = argv[index + 1];
  if (!values["--report"] || !values["--output"]) throw new Error("--report and --output are required");
  return values;
}

async function artifactTool() {
  const nodeModules = process.env.TEENI_NODE_MODULES;
  if (!nodeModules) throw new Error("TEENI_NODE_MODULES is required");
  const require = createRequire(import.meta.url);
  const resolved = require.resolve("@oai/artifact-tool", { paths: [nodeModules] });
  return import(pathToFileURL(resolved).href);
}

async function freezeDetailHeaders(filePath, sheetOrder, names) {
  const nodeModules = process.env.TEENI_NODE_MODULES;
  if (!nodeModules) throw new Error("TEENI_NODE_MODULES is required");
  const require = createRequire(import.meta.url);
  const JSZip = require(require.resolve("jszip", { paths: [nodeModules] }));
  const zip = await JSZip.loadAsync(await fs.readFile(filePath));
  for (const name of names) {
    const index = sheetOrder.indexOf(name) + 1;
    if (index < 1) throw new Error(`missing detail sheet: ${name}`);
    const entry = zip.file(`xl/worksheets/sheet${index}.xml`);
    if (!entry) throw new Error(`missing worksheet XML: ${name}`);
    const xml = await entry.async("string");
    if (/<(?:\w+:)?pane\b[^>]*\bstate=["']frozen["'][^>]*\/?\s*>/.test(xml)) continue;
    const pane = (prefix) => `<${prefix ? `${prefix}:` : ""}pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen" />`;
    let next = xml.replace(
      /<((?:(\w+):)?sheetView)\b([^>]*)\/>/,
      (_match, tag, prefix, attributes) => `<${tag}${attributes}>${pane(prefix)}</${tag}>`,
    );
    if (next === xml) {
      next = xml.replace(
        /<((?:(\w+):)?sheetView)\b([^>]*)>/,
        (match, _tag, prefix) => `${match}${pane(prefix)}`,
      );
    }
    if (next === xml) throw new Error(`cannot freeze detail header: ${name}`);
    zip.file(entry.name, next);
  }
  const stagedPath = `${filePath}.freeze-${process.pid}-${Date.now()}.xlsx`;
  await fs.writeFile(stagedPath, await zip.generateAsync({ type: "uint8array", compression: "DEFLATE" }));
  await fs.rename(stagedPath, filePath);
}

function safe(value) {
  if (typeof value !== "string") return value;
  return /^[=+\-@]/.test(value) ? `'${value}` : value;
}

function normalizeRows(rows) {
  const width = Math.max(1, ...rows.map((row) => row.length));
  return rows.map((row) => [...row.map(safe), ...Array(width - row.length).fill("")]);
}

function summaryRows(report) {
  const p = report.primary;
  const rawBands = new Map(p.diagnostics.rawBands.map((item) => [item.label, item.count]));
  const validBands = new Map(p.validCohort.bands.map((item) => [item.label, item.count]));
  const labels = ["0轮", "1轮", "2-4轮", "5-9轮", "10轮以上"];
  return [
    ["Teeni 基础分析结论总览"],
    ["纯本地分析；参与深度、净有效深度、质量、安全和结束信号保持独立口径。"],
    [],
    ["规模指标", "值", "口径", "", "参与指标", "值", "口径", "", "风险指标", "数量", "候选率", "独立分母"],
    ["总轮数", p.summary.rows, "原始导出行", "", "非模板参与率", p.filteredCohort.retainedShare, "至少一条非模板输入", "", "唯一质量候选回复", p.diagnostics.uniqueQualityCandidateResponses, p.diagnostics.qualityRate, `可评分AI回复 ${p.diagnostics.scoreableRows}`],
    ["会话数", p.summary.sessions, "cid 去重", "", "平均净有效轮数", p.validCohort.avgMeaningfulTurns, "有效会话均值", "", "AI安全候选", p.summary.safetyCandidates, p.diagnostics.safetyRate, `可评分AI回复 ${p.diagnostics.scoreableRows}`],
    ["用户数", p.summary.users, "clientId 去重", "", "净有效五轮+占比", p.validCohort.fivePlusShare, "净有效轮数至少 5", "", "用户风险表达候选", p.summary.userRiskExpressions, p.summary.userRiskExpressionRate, `可审核query ${p.summary.reviewableUserQueries}`],
    ["", "", "", "", "", "", "", "", "需关注用户候选", p.summary.attentionUsers, p.summary.attentionUserRate, `有可审核query用户 ${p.summary.reviewableQueryUsers}`],
    ["", "", "", "", "", "", "", "", "P0 / P1 / P2", `${p.summary.attentionP0} / ${p.summary.attentionP1} / ${p.summary.attentionP2}`, "", "仅表示人工审核顺序"],
    ["", "", "", "", "", "", "", "", "AI解析失败", p.summary.parseFailures, "", "response 解析"],
    [],
    ["轮数区间", "原始会话", "净有效会话", "", "口径提示", "说明", "", "", "", "", "", ""],
    ...labels.map((label, index) => [
      label, rawBands.get(label) ?? 0, validBands.get(label) ?? 0, "",
      index === 0 ? "结束信号" : "",
      index === 0 ? "有效与未剔除口径并列，不推断真实离开原因" : "",
    ]),
    [],
    ["画像字段", "覆盖率", "口径"],
    ["城市覆盖率", p.locationAnalysis?.coverage ?? "", "所在地解析正常用户 / 全部独立用户"],
    ["生日可解析率", p.constellationAnalysis?.coverage ?? "", "生日解析正常用户 / 全部独立用户"],
    [], [], [], [], [], [], [],
    ["效果与行为补充指标", "", "", "", "分子", "分母", "比例", "", "观察限制"],
    ...[["真实请求缺失回复文本", p.supplementalMetrics.quality.responseMissing],
      ["真实请求解析失败", p.supplementalMetrics.quality.responseParseFailure],
      ["兜底后观察到继续", p.supplementalMetrics.continuationProxies.fallback],
      ["纠错候选后观察到继续", p.supplementalMetrics.continuationProxies.correction]]
      .map(([label, value]) => [label, "", "", "", value.numerator, value.denominator, value.rate, "", "当日文件代理；继续不等于恢复或满意"]),
    ["每百条可评分回复质量候选事件数", "", "", "", p.supplementalMetrics.quality.qualityCandidateEventsPer100Responses, "", "", "", "事件密度，可超过100"],
    ["开场去重曝光", "", "", "", p.supplementalMetrics.openingFunnel.exposures, "", "", "", "仅合并连续固定开场重试"],
    ...["realUserReplies", "firstEffectiveResponses", "effectiveThreeTurns", "effectiveFiveTurns"].map((key, index) => [
      ["真实用户接话", "首次有效回应", "有效3轮", "有效5轮"][index], "", "", "", p.supplementalMetrics.openingFunnel[key],
      p.supplementalMetrics.openingFunnel.exposures, p.supplementalMetrics.openingFunnel[key + "Rate"]]),
    ["5分钟成熟样本重开代理", "", "", "", p.supplementalMetrics.fallbackReopen.matureReopenedSessions,
      p.supplementalMetrics.fallbackReopen.matureTriggerSessions, p.supplementalMetrics.fallbackReopen.matureSessionReopenRate, "",
      `未成熟 ${p.supplementalMetrics.fallbackReopen.immatureTriggerSessions}；最大记录时间代理，未确认导出水位`],
    [],
    ["首次真实输入后轮次", "", "", "", "到达会话", "观察到继续", "未观察到继续", "", "缺失回复文本", "质量摩擦候选"],
    ...p.supplementalMetrics.earlyExperience.map((item) => [item.turn, "", "", "", item.reachedSessions, item.continuedSessions,
      item.observedStoppedSessions, "", item.missingResponseSessions, item.frictionCandidateSessions]),
    [],
    ["会话结构指标", "", "", "", "分子", "分母", "数值", "", "当天可见记录；不依赖开场文案"],
    ...[["平均会话轮次", "totalTurns", "totalSessions", "averageTurns"],
      ["多轮会话率", "multiTurnSessions", "totalSessions", "multiTurnRate"],
      ["多轮会话平均轮次", "multiTurnTurns", "multiTurnSessions", "multiTurnAverageTurns"],
      ["5轮及以上会话占比", "fivePlusSessions", "totalSessions", "fivePlusRate"]]
      .map(([label, numerator, denominator, value]) => [label, "", "", "", p.sessionStructure[numerator], p.sessionStructure[denominator], p.sessionStructure[value]]),
  ];
}

function cohortRows(title, note, full, cohort) {
  const rawFivePlus = (full.rawBands[2].count + full.rawBands[3].count) / Math.max(1, full.sessions);
  return [
    [title], [note], [],
    ["指标", "原始口径", "当前口径", "变化"],
    ["轮次数", full.rows, cohort.rows, cohort.rows - full.rows],
    ["会话数", full.sessions, cohort.sessions, cohort.sessions - full.sessions],
    ["用户数", full.users, cohort.users, cohort.users - full.users],
    ["平均轮数", full.avgRawTurns, cohort.avgMeaningfulTurns, cohort.avgMeaningfulTurns - full.avgRawTurns],
    ["中位轮数", full.medianRawTurns, cohort.medianMeaningfulTurns, cohort.medianMeaningfulTurns - full.medianRawTurns],
    ["五轮以上占比", rawFivePlus, cohort.fivePlusShare, cohort.fivePlusShare - rawFivePlus],
    ["唯一质量候选回复率", full.qualityRate, cohort.qualityRate, full.qualityRate == null || cohort.qualityRate == null ? "" : cohort.qualityRate - full.qualityRate],
    ["安全候选率", full.safetyRate, cohort.safetyRate, cohort.safetyRate - full.safetyRate],
    [],
    ["轮数区间", "会话数"],
    ...cohort.bands.map((item) => [item.label, item.count]),
  ];
}

function endingRows(analysis, raw = false) {
  const turnLabel = analysis.turnLabel;
  const rows = [
    [raw ? "五轮以上会话可观察结束信号（不剔除无效）" : "五轮以上会话可观察结束信号"],
    [raw
      ? "按原始轮数至少 5 轮入组并取原始末轮；末轮可能是无效模板，只描述可观察信号。"
      : "按净有效轮数至少 5 轮入组并取最后有效轮；只描述可观察信号，不推断真实离开原因。"],
    [],
    ["五轮以上会话", analysis.cohortSessions, "10轮以上", analysis.tenPlusSessions, "导出边界风险", analysis.boundaryRiskSessions, "质量信号会话", analysis.qualitySignalSessions],
    ["AI安全信号会话", analysis.aiSafetySignalSessions, "用户风险信号会话", analysis.userRiskSignalSessions, "兼容安全并集会话", analysis.safetySignalSessions],
    ["结束信号主类", "会话数", "占比", `平均${turnLabel}`, "AI继续追问占比", "边界风险数"],
    ...analysis.categories.map((item) => [item.category, item.count, item.rate, item.avgTurns, item.aiFollowupShare, item.boundaryRisk]),
  ];
  if (raw) {
    const reopen = analysis.fallbackReopen;
    rows.push(
      [],
      ["对/嗯固定兜底后的五分钟新会话"],
      ["同一用户在原始末轮命中“对/嗯 + 固定兜底”后，于 (0, 300] 秒开启不同 cid；这是聊天延续意愿的行为代理，不等于真实动机或强烈聊天欲望。"],
      [],
      ["触发会话", reopen.triggerSessions, "触发用户", reopen.triggerUsers, "5分钟内新开会话", reopen.reopenedSessions, "新开会话用户", reopen.reopenedUsers],
      ["已观察会话新开比例", reopen.sessionReopenRate, "已观察用户新开比例", reopen.userReopenRate, "窗口（秒）", reopen.windowSeconds],
      [],
      ["触发词", "触发会话", "触发用户", "5分钟内新开会话", "新开会话用户", "会话级新开率", "用户级新开率"],
      ...reopen.byQuery.map((item) => [item.query, item.triggerSessions, item.triggerUsers, item.reopenedSessions, item.reopenedUsers, item.sessionReopenRate, item.userReopenRate]),
      [],
      ["下一会话时间分布（会话级）"],
      ["区间采用左开右闭边界；占全部触发会话与占已观察到下一会话使用不同分母。未观察到下一会话包含右截断样本，不代表永久流失。"],
      ["新开间隔", "对", "嗯", "总会话数", "占全部触发会话", "占已观察到下一会话", "可判定样本数"],
      ...reopen.delayBands.map((item) => [item.label, item.duiCount, item.enCount, item.count, item.triggerRate, item.observedReopenRate, item.observableSessions]),
      [],
      ["观察限制", `已观察到下一会话 ${reopen.observedReopenSessions} 个；拥有完整 24 小时无事件观察期 ${reopen.complete24HourObservationSessions} 个。24小时以上为开放区间，不提供可判定分母。`],
    );
  }
  rows.push([], ["真实主意图", "数量", "占比", "真实子意图", "数量", "占比"]);
  const count = Math.max(analysis.mainIntents.length, analysis.subIntents.length);
  for (let index = 0; index < count; index += 1) {
    const main = analysis.mainIntents[index];
    const sub = analysis.subIntents[index];
    rows.push([main?.label ?? "", main?.count ?? "", main?.rate ?? "", sub?.label ?? "", sub?.count ?? "", sub?.rate ?? ""]);
  }
  rows.push([], ["代表案例", "置信度", "cid", turnLabel, "分类证据", "末轮query", "AI回复预览"]);
  rows.push(...analysis.examples.map((item) => [item.category, item.confidence, item.cid, item.turns, item.evidence, item.query, item.responsePreview]));
  return rows;
}

const ENDING_HEADERS = [
  "clientId", "cid", "原始轮数", "非模板轮数", "净有效轮数", "会话开始时间", "会话结束时间",
  "末轮源行", "末轮时间戳", "真实主意图", "真实子意图", "意图解析状态", "结束信号主类", "置信度",
  "分类证据", "末轮query", "末轮AI response", "AI继续追问", "导出边界风险", "末轮质量信号", "末轮安全信号",
  "末轮AI安全信号", "末轮用户风险信号", "末轮用户复核优先级",
];

function endingDetailRows(items, raw = false) {
  const headers = raw
    ? [
      ...ENDING_HEADERS.slice(0, 9), "末轮是否无效", "末轮无效原因", ...ENDING_HEADERS.slice(9),
      "是否命中对/嗯固定兜底", "触发词", "固定兜底类型", "固定兜底文本", "下一会话cid",
      "下一会话开始时间", "间隔秒数", "5分钟内新开会话",
    ]
    : ENDING_HEADERS;
  return [headers, ...items.map((item) => headers.map((header) => item[header] ?? ""))];
}

function subIntentRows(p) {
  const c = p.subIntentComparison;
  return [
    ["有效对话子意图对比"],
    ["比较全部净有效轮次与五轮以上有效末轮；百分点差=末轮占比-全部占比。"], [],
    ["真实子意图", "全部净有效轮次", "全部占比", "五轮末轮会话", "末轮占比", "百分点差"],
    ...c.rows.map((item) => [item.label, item.allCount, item.allRate, item.endingCount, item.endingRate, item.ppDifference]),
  ];
}

function intentRows(p) {
  const rows = [
    ["真实意图分析"], [`意图来源：${p.intentAnalysis.source}。488/901/904 只读取 response.extra.intent_name。`], [],
    ["解析状态", "数量", "占比"],
    ...p.intentAnalysis.status.map((item) => [item.status, item.count, item.rate]),
    [], ["主意图", "数量", "占比", "子意图", "数量", "占比"],
  ];
  const count = Math.max(p.intentAnalysis.main.length, p.intentAnalysis.sub.length);
  for (let index = 0; index < count; index += 1) {
    const main = p.intentAnalysis.main[index];
    const sub = p.intentAnalysis.sub[index];
    rows.push([main?.label ?? "", main?.count ?? "", main?.rate ?? "", sub?.label ?? "", sub?.count ?? "", sub?.rate ?? ""]);
  }
  return rows;
}

function demographicRows(p) {
  const d = p.demographicAnalysis;
  return [
    ["年龄性别概览"],
    [`按 clientId 解析稳定属性；冲突或非法值不强行归类。少于 ${d.minimumSegmentUsers} 个独立用户的分组只作审计。`], [],
    ["独立用户", d.users, "属性正常用户", d.normalUsers, "属性覆盖率", d.coverage], [],
    ["属性状态", "用户数"], ...d.status.map((item) => [item.status, item.users]), [],
    ["年龄段", "性别", "用户数", "会话数", "净有效轮次", "平均净有效轮数", "五轮以上会话", "五轮以上占比", "样本状态"],
    ...d.distribution.map((item) => [item.ageBand, item.gender, item.users, item.sessions, item.netValidTurns, item.avgNetValidTurns, item.fivePlusSessions, item.fivePlusShare, item.users >= d.minimumSegmentUsers ? "可描述" : "样本不足"]),
  ];
}

function profileSegmentRows(title, note, analysis, groupLabel) {
  return [
    [title],
    [`${note}；按 clientId 解析稳定属性，冲突或非法值不强行归类。少于 ${analysis.minimumSegmentUsers} 个独立用户的分组标记为样本不足。`], [],
    ["独立用户", analysis.users, "解析正常用户", analysis.normalUsers, "字段覆盖率", analysis.coverage], [],
    ["解析状态", "用户数"], ...analysis.status.map((item) => [item.status, item.users]), [],
    [groupLabel, "独立用户", "正常用户内构成比", "会话数", "净有效轮次", "平均净有效轮数", "五轮以上会话", "五轮以上占比", "样本状态"],
    ...analysis.distribution.map((item) => [
      item.label, item.users, item.userShare, item.sessions, item.netValidTurns,
      item.avgNetValidTurns, item.fivePlusSessions, item.fivePlusShare,
      item.users >= analysis.minimumSegmentUsers ? "可描述" : "样本不足",
    ]),
  ];
}

function issueRows(title, note, items) {
  return [[title], [note], [], ["审核对象", "类别", "严重度", "数量", "会话", "用户", "候选率", "独立分母", "证据", "建议"],
    ...items.map((item) => [item.reviewTarget, item.category, item.severity, item.count, item.sessions, item.users, item.rate, item.denominator, item.example, item.suggestion])];
}

function detailIssueRows(title, note, items) {
  return [[title], [note], [], ["类别", "严重度", "id", "clientId", "cid", "证据", "用户文本", "AI文本"],
    ...items.slice(0, 500).map((item) => [item.category, item.severity, item.record_id, item.client_id, item.cid, item.evidence, item.userText, item.aiText])];
}

function attentionUserRows(items) {
  const headers = ["优先级", "clientId", "命中query数", "命中会话数", "类别数", "类别集合", "最高严重度", "首次命中时间", "最后命中时间", "典型证据", "分级原因"];
  return [headers, ...items.map((item) => [
    item.reviewPriority, item.clientId, item.hitQueries, item.hitSessions, item.categoryCount,
    item.categories, item.highestSeverity, item.firstHitTime, item.lastHitTime, item.example,
    item.priorityReason,
  ])];
}

function userRiskDetailRows(items) {
  const headers = ["优先级", "类别", "严重度", "id", "clientId", "cid", "时间", "轮次", "证据", "用户query", "对应AI response", "AI安全信号", "待复核状态"];
  return [headers, ...items.map((item) => [
    item.reviewPriority, item.category, item.severity, item.record_id, item.client_id, item.cid,
    item.source_time, item.turn_index, item.evidence, item.userText, item.aiText,
    item.aiSafetySignals, item.status,
  ])];
}

function openingRows(p) {
  return [["开场原始记录接续分析"], ["历史原始记录接续，可能含自动模板；真实用户有效漏斗见结论总览。"], [],
    ["难度", "原始曝光", "后续有记录", "记录接续率", "强延续候选", "理解困难", "换题", "原始三轮", "原始五轮"],
    ...p.openingByDifficulty.map((item) => [item.group, item.exposures, item.opened, item.exposures ? item.opened / item.exposures : 0, item.strong, item.understanding, item.switched, item.thirdTurn, item.fifthTurn]),
    [], ["问题类型", "原始曝光", "后续有记录", "记录接续率", "强延续候选", "理解困难", "换题", "原始三轮", "原始五轮"],
    ...p.openingByType.map((item) => [item.group, item.exposures, item.opened, item.exposures ? item.opened / item.exposures : 0, item.strong, item.understanding, item.switched, item.thirdTurn, item.fifthTurn])];
}

function openingDetailRows(p) {
  const headers = ["id", "clientId", "cid", "开场文本", "AI回复", "后续有记录", "延续标签", "理解难度", "问题类型", "理解困难", "原始三轮", "原始五轮"];
  return [headers, ...p.openings.map((item) => [item.id, item.client_id, item.cid, item.opening_text, item.ai_text, item.opened ? "是" : "否", item.label, item.difficulty, item.question_type, item.understanding_difficulty ? "是" : "否", item.third_turn_continued ? "是" : "否", item.fifth_turn_continued ? "是" : "否"])];
}

function sessionRows(p) {
  return [["会话汇总"], ["按原始轮数排序展示；完整逐轮数据见基础逐轮明细 CSV。"], [],
    ["clientId", "cid", "原始轮数", "非模板轮数", "净有效轮数", "真实参与", "开始时间", "结束时间", "主意图", "纠错信号", "复读", "空回复"],
    ...p.sessionSummaries.slice(0, 400).map((item) => [item.clientId, item.cid, item.turns, item.meaningfulTurns, item.netValidTurns, item.engaged ? "是" : "否", item.start, item.end, item.mainIntention, item.correctionSignals, item.exactRepeats, item.missingText])];
}

function comparisonRows(report) {
  const p = report.primary;
  const b = report.baseline;
  return [["版本基准对比"], ["采样窗口、用户和意图体系可能不同，差异不能直接归因于版本。"], [],
    ["指标", b.label, p.label, "差值"],
    ["总轮数", b.summary.rows, p.summary.rows, p.summary.rows - b.summary.rows],
    ["会话数", b.summary.sessions, p.summary.sessions, p.summary.sessions - b.summary.sessions],
    ["用户数", b.summary.users, p.summary.users, p.summary.users - b.summary.users],
    ["真实参与率", b.filteredCohort.retainedShare, p.filteredCohort.retainedShare, p.filteredCohort.retainedShare - b.filteredCohort.retainedShare],
    ["平均净有效轮数", b.validCohort.avgMeaningfulTurns, p.validCohort.avgMeaningfulTurns, p.validCohort.avgMeaningfulTurns - b.validCohort.avgMeaningfulTurns],
    ["净有效五轮+占比", b.validCohort.fivePlusShare, p.validCohort.fivePlusShare, p.validCohort.fivePlusShare - b.validCohort.fivePlusShare]];
}

function methodologyRows(report) {
  return [["分析口径"], ["基础分析全程本地运行，不调用任何外部模型。"], [], ["项目", "内容"],
    ["报告模型", report.schemaVersion], ["核心版本", report.coreVersion], ["规则版本", report.rulesVersion],
    ["基础明细契约", report.detailContractVersion],
    ["净有效轮次", "逐轮排除配置中的用户或 AI 无效模板；混合会话保留有效轮"],
    ["真实意图", "488/901/904 仅从 response.extra.intent_name 严格解析，其他场景使用源意图列"],
    ["人口属性", "按 clientId 聚合；缺失、非法、冲突分开审计；18+ 仅审计"],
    ["所在地", "488/901/904 从 response.extra.city 读取；NFKC、首尾空白清理并移除一个末尾“市”，不做省级映射；分组占比以城市正常用户为分母"],
    ["星座", "488/901/904 从 response.extra.birthday 读取；仅接受合法 YYYY-MM-DD 公历日期并按西方十二星座常用边界归类；分组占比以生日正常用户为分母"],
    ["有效结束信号", "净有效轮数至少 5，取最后有效轮；不推断真实离开动机"],
    ["未剔除结束信号", "原始轮数至少 5，取原始末轮并保留末轮无效状态与原因"],
    ["对/嗯兜底后新会话", "原始末轮严格命中配置的对/嗯及固定兜底；连接同一 clientId 随后的不同 cid。五分钟 KPI 采用 (0, 300] 秒；时间分布采用会话级双分母并保留右截断限制；仅作聊天延续意愿行为代理"],
    ["AI安全", "仅审核AI response；高召回自动候选，不等于人工审核结论"],
    ["用户query风险", "审核所有非空且非固定/注入开场或确定无效模板的用户query；独立规则、分母和工作表，不因AI回复异常跳过"],
    ["用户复核优先级", "P0/P1/P2只表示人工审核顺序；规则命中称为风险表达候选或需关注用户候选，不代表危险用户已确认"],
    ["话题", "不在本报告中处理；使用 analyze-teeni-topics 对基础包单独预检和分类"]];
}

function buildSpecs(report) {
  const p = report.primary;
  const specs = new Map([
    ["结论总览", summaryRows(report)],
    ["剔除单轮开场白后分析", cohortRows("剔除单轮开场白后分析", "只移除没有任何非模板用户输入的会话。", p.diagnostics, p.filteredCohort)],
    ["剔除无效轮次后分析", cohortRows("剔除无效轮次后分析", "逐轮排除确认无效内容，净有效轮归零才排除整个会话。", p.diagnostics, p.validCohort)],
    ["五轮以上结束分析", endingRows(p.endingAnalysis)],
    ["五轮以上结束分析（不剔除无效）", endingRows(p.rawEndingAnalysis, true)],
    ["五轮以上结束明细", endingDetailRows(p.endingDetails)],
    ["五轮以上结束明细（不剔除无效）", endingDetailRows(p.rawEndingDetails, true)],
    ["有效对话子意图对比", subIntentRows(p)], ["真实意图分析", intentRows(p)],
    ["新版问题总览", issueRows("问题总览", "AI质量、AI安全和用户query风险采用独立审核对象与分母。", [
      ...p.qualitySummary.map((item) => ({ ...item, reviewTarget: "AI质量", denominator: `全部原始记录 ${p.summary.rows}（含开场/缺失）` })),
      ...p.safetySummary.map((item) => ({ ...item, reviewTarget: "AI安全", denominator: `可评分AI回复 ${p.diagnostics.scoreableRows}` })),
      ...p.userSafetySummary.map((item) => ({ ...item, reviewTarget: "用户query风险", denominator: `可审核用户query ${p.summary.reviewableUserQueries}` })),
    ])],
    ["新版开场分析", openingRows(p)], ["新版开场明细", openingDetailRows(p)],
    ["新版质量问题", detailIssueRows("质量问题", "最多展示 500 条代表记录；完整逐轮信号见基础明细。", p.qualityIssues)],
    ["新版安全问题", detailIssueRows("AI response安全问题", "审核对象仅为AI response；高召回规则候选，必须人工复核。", p.safetyIssues)],
    ["需关注用户汇总", attentionUserRows(p.attentionUsers)],
    ["用户风险表达明细", userRiskDetailRows(p.userSafetyIssues)],
    ["优化指标体系", [["优化指标体系"], ["基础指标相互独立，不合并为单一分数。"], [], ["模块", "指标", "定义", "计算", "方向", "范围"], ...report.metricsFramework]],
    ["产品迭代建议", [["产品迭代建议"], ["建议只基于本地可观察信号。"], [], ["优先级", "方向", "证据", "动作", "观察指标"], ...p.recommendations]],
    ["新版会话汇总", sessionRows(p)], ["分析口径", methodologyRows(report)],
  ]);
  if (p.demographicAnalysis) specs.set("年龄性别概览", demographicRows(p));
  if (p.locationAnalysis) specs.set("所在地分析", profileSegmentRows(
    "所在地分析",
    "城市仅做 NFKC、首尾空白清理并移除一个末尾“市”；地区、自治州、盟、特别行政区不做省级映射",
    p.locationAnalysis,
    "城市",
  ));
  if (p.constellationAnalysis) specs.set("星座分析", profileSegmentRows(
    "星座分析",
    "生日只接受合法 YYYY-MM-DD 公历日期，星座采用西方十二星座常用边界",
    p.constellationAnalysis,
    "星座",
  ));
  if (report.baseline) specs.set("版本基准对比", comparisonRows(report));
  return specs;
}

function setWidths(sheet, widths, rowCount) {
  widths.forEach((width, index) => {
    sheet.getRangeByIndexes(0, index, rowCount, 1).format.columnWidth = width;
  });
}

function styleAnalysisSheet(sheet, rows, name) {
  const rowCount = rows.length;
  const colCount = rows[0].length;
  const used = sheet.getRangeByIndexes(0, 0, rowCount, colCount);
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(Math.min(4, rowCount));
  used.format = { font: { name: "Microsoft YaHei", size: 10, color: COLORS.ink }, verticalAlignment: "center" };
  used.format.borders = { insideHorizontal: { style: "thin", color: COLORS.line } };
  sheet.getRangeByIndexes(0, 0, 1, colCount).merge();
  sheet.getCell(0, 0).format = { fill: COLORS.navy, font: { name: "Microsoft YaHei", size: 18, bold: true, color: COLORS.white }, rowHeight: 42 };
  if (rowCount > 1) {
    sheet.getRangeByIndexes(1, 0, 1, colCount).merge();
    sheet.getCell(1, 0).format = { fill: COLORS.blueSoft, font: { name: "Microsoft YaHei", size: 10, color: "#415466" }, rowHeight: 34, wrapText: true };
  }
  for (let row = 2; row < rowCount; row += 1) {
    const values = rows[row];
    const previousBlank = rows[row - 1]?.every((value) => value === "");
    const stringsOnly = values.every((value) => typeof value === "string" || value === "");
    if ((row === 3 || previousBlank) && values.filter((value) => value !== "").length > 1 && stringsOnly) {
      sheet.getRangeByIndexes(row, 0, 1, colCount).format = { fill: COLORS.teal, font: { name: "Microsoft YaHei", size: 10, bold: true, color: COLORS.white }, rowHeight: 28, wrapText: true };
    }
  }
  used.format.wrapText = false;
  const defaults = Array(colCount).fill(14);
  if (/结束分析/.test(name)) Object.assign(defaults, { 0: 26, 1: 13, 2: 13, 3: 16, 4: 18, 5: 20, 6: 28 });
  if (/问题|建议|指标体系/.test(name)) Object.assign(defaults, { 0: 20, 1: 18, 6: 18, 7: 24, 8: 42, 9: 42 });
  if (name === "分析口径") Object.assign(defaults, { 0: 20, 1: 72 });
  if (name === "新版会话汇总") Object.assign(defaults, { 0: 20, 1: 24, 6: 20, 7: 20, 8: 22 });
  if (name === "所在地分析" || name === "星座分析") Object.assign(defaults, { 0: 24, 1: 16, 2: 20, 3: 15, 4: 16, 5: 18, 6: 18, 7: 18, 8: 16 });
  setWidths(sheet, defaults, rowCount);
  if (/结束分析/.test(name)) sheet.getRangeByIndexes(0, 4, rowCount, Math.min(3, colCount - 4)).format.wrapText = true;
  if (/问题|建议|指标体系|分析口径/.test(name)) used.format.wrapText = true;
}

function styleDetailSheet(sheet, rows, name) {
  const rowCount = rows.length;
  const colCount = rows[0].length;
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  const used = sheet.getRangeByIndexes(0, 0, rowCount, colCount);
  used.format = { font: { name: "Microsoft YaHei", size: 9, color: COLORS.ink }, verticalAlignment: "top" };
  sheet.getRangeByIndexes(0, 0, 1, colCount).format = { fill: COLORS.teal, font: { name: "Microsoft YaHei", size: 9, bold: true, color: COLORS.white }, rowHeight: 30, wrapText: true, verticalAlignment: "center" };
  const widths = rows[0].map((header) => {
    if (/query|response|分类证据|无效原因|兜底文本|类别集合|分级原因|典型证据|证据/.test(String(header))) return 38;
    if (/clientId|cid|时间|意图/.test(String(header))) return 22;
    if (/类别/.test(String(header))) return 24;
    if (/优先级|严重度|状态/.test(String(header))) return 14;
    if (/信号/.test(String(header))) return 28;
    return 13;
  });
  setWidths(sheet, widths, rowCount);
  for (let col = 0; rowCount > 1 && col < colCount; col += 1) {
    if (/query|response|分类证据|无效原因|兜底文本|信号|类别集合|分级原因|典型证据|证据/.test(String(rows[0][col]))) {
      sheet.getRangeByIndexes(1, col, Math.max(0, rowCount - 1), 1).format.wrapText = true;
    }
  }
  if (fixedDetailRowHeights.has(name) && rowCount > 1) {
    sheet.getRangeByIndexes(1, 0, rowCount - 1, colCount).format.rowHeight = fixedDetailRowHeights.get(name);
  }
}

function columnName(count) {
  let value = count;
  let output = "";
  while (value > 0) {
    value -= 1;
    output = String.fromCharCode(65 + (value % 26)) + output;
    value = Math.floor(value / 26);
  }
  return output;
}

function addTable(sheet, range, name, style = "TableStyleMedium2") {
  const table = sheet.tables.add(range, true, name);
  table.style = style;
  table.showFilterButton = true;
}

const options = parseArgs(process.argv.slice(2));
const report = JSON.parse(await fs.readFile(path.resolve(options["--report"]), "utf8"));
if (report.schemaVersion !== "teeni-base-report-model/2.6.0") throw new Error("unsupported report model");
const { Workbook, SpreadsheetFile } = await artifactTool();
const workbook = Workbook.create();
const specs = buildSpecs(report);
const sheets = new Map();
const detailNames = new Set(["五轮以上结束明细", "五轮以上结束明细（不剔除无效）", "新版开场明细", "需关注用户汇总", "用户风险表达明细"]);
const fixedDetailRowHeights = new Map([["新版开场明细", 42], ["需关注用户汇总", 42]]);
for (const name of report.sheetOrder) {
  const raw = specs.get(name);
  if (!raw) throw new Error(`missing content for sheet: ${name}`);
  const rows = normalizeRows(raw);
  const sheet = workbook.worksheets.add(name);
  sheet.getRangeByIndexes(0, 0, rows.length, rows[0].length).values = rows;
  if (detailNames.has(name)) styleDetailSheet(sheet, rows, name);
  else styleAnalysisSheet(sheet, rows, name);
  sheets.set(name, { sheet, rows });
}

const summary = sheets.get("结论总览").sheet;
summary.getRange("A4:C4").format = { fill: COLORS.tealSoft, font: { bold: true, color: COLORS.teal } };
summary.getRange("E4:G4").format = { fill: COLORS.blueSoft, font: { bold: true, color: "#205B7A" } };
summary.getRange("I4:L4").format = { fill: COLORS.amberSoft, font: { bold: true, color: "#8A5A00" } };
summary.getRange("F5").format.numberFormat = "0.0%";
summary.getRange("F6").format.numberFormat = "0.0";
summary.getRange("F7").format.numberFormat = "0.0%";
summary.getRange("K5:K8").format.numberFormat = "0.0%";
const profileCoverageHeader = sheets.get("结论总览").rows.findIndex((row) => row[0] === "画像字段") + 1;
if (profileCoverageHeader > 0) summary.getRange(`B${profileCoverageHeader + 1}:B${profileCoverageHeader + 2}`).format.numberFormat = "0.0%";
setWidths(summary, [18, 14, 24, 3, 22, 14, 25, 3, 22, 14, 14, 27], summaryRows(report).length);
const supplementalHeader = sheets.get("结论总览").rows.findIndex((row) => row[0] === "效果与行为补充指标") + 1;
const earlyHeader = sheets.get("结论总览").rows.findIndex((row) => row[0] === "首次真实输入后轮次") + 1;
for (let row = supplementalHeader; row <= sheets.get("结论总览").rows.length; row += 1) {
  summary.getRange(`A${row}:C${row}`).merge();
  summary.getRange(`A${row}:L${row}`).format.rowHeight = 30;
  if (row < earlyHeader) {
    summary.getRange(`I${row}:L${row}`).merge();
    summary.getRange(`I${row}`).format.wrapText = true;
  }
}
summary.getRange(`G${supplementalHeader + 1}:G${earlyHeader - 2}`).format.numberFormat = "0.0%";
summary.getRange(`E${supplementalHeader + 5}`).format.numberFormat = "0.0";
const structureHeader = sheets.get("结论总览").rows.findIndex((row) => row[0] === "会话结构指标") + 1;
summary.getRange(`G${structureHeader + 1}:G${structureHeader + 4}`).format.numberFormat = "0.00";
summary.getRange(`G${structureHeader + 2}`).format.numberFormat = "0.00%";
summary.getRange(`G${structureHeader + 4}`).format.numberFormat = "0.00%";
const depthHeader = sheets.get("结论总览").rows.findIndex((row) => row[0] === "轮数区间") + 1;
const summaryChart = summary.charts.add("bar", summary.getRange(`A${depthHeader}:C${depthHeader + 5}`));
summaryChart.title = "会话深度：原始 vs 净有效";
summaryChart.hasLegend = true;
summaryChart.setPosition(`E${depthHeader}`, `L${depthHeader + 14}`);

for (const name of ["剔除单轮开场白后分析", "剔除无效轮次后分析"]) {
  const sheet = sheets.get(name).sheet;
  sheet.getRange("B8:D9").format.numberFormat = "0.0";
  sheet.getRange("B10:D12").format.numberFormat = "0.0%";
  const chart = sheet.charts.add("bar", sheet.getRange("A14:B19"));
  chart.title = name === "剔除无效轮次后分析" ? "净有效轮数分布" : "非模板轮数分布";
  chart.hasLegend = false;
  chart.setPosition("F4", "M18");
}

for (const [name, analysis] of [["五轮以上结束分析", report.primary.endingAnalysis], ["五轮以上结束分析（不剔除无效）", report.primary.rawEndingAnalysis]]) {
  const entry = sheets.get(name);
  const sheet = entry.sheet;
  const categoryHeader = entry.rows.findIndex((row) => row[0] === "结束信号主类") + 1;
  const categoryEnd = categoryHeader + analysis.categories.length;
  sheet.getRange(`C${categoryHeader + 1}:C${categoryEnd}`).format.numberFormat = "0.0%";
  sheet.getRange(`D${categoryHeader + 1}:D${categoryEnd}`).format.numberFormat = "0.0";
  sheet.getRange(`E${categoryHeader + 1}:E${categoryEnd}`).format.numberFormat = "0.0%";
  const intentHeader = entry.rows.findIndex((row) => row[0] === "真实主意图") + 1;
  const intentStart = intentHeader + 1;
  const intentCount = Math.max(analysis.mainIntents.length, analysis.subIntents.length);
  if (intentCount > 0) {
    const intentEnd = intentStart + intentCount - 1;
    sheet.getRange(`C${intentStart}:C${intentEnd}`).format.numberFormat = "0.0%";
    sheet.getRange(`F${intentStart}:F${intentEnd}`).format.numberFormat = "0.0%";
  }
  const chart = sheet.charts.add("bar", sheet.getRange(`A${categoryHeader}:B${categoryEnd}`));
  chart.title = "可观察结束信号";
  chart.hasLegend = false;
  chart.setPosition("I5", "P19");
  if (name === "五轮以上结束分析（不剔除无效）") {
    const proxyTitleRow = entry.rows.findIndex((row) => row[0] === "对/嗯固定兜底后的五分钟新会话") + 1;
    sheet.getRange(`A${proxyTitleRow}:H${proxyTitleRow}`).merge();
    sheet.getRange(`A${proxyTitleRow}:H${proxyTitleRow}`).format = {
      fill: COLORS.tealSoft, font: { name: "Microsoft YaHei", size: 12, bold: true, color: COLORS.teal }, rowHeight: 30,
    };
    sheet.getRange(`A${proxyTitleRow + 1}:H${proxyTitleRow + 1}`).merge();
    sheet.getRange(`A${proxyTitleRow + 1}:H${proxyTitleRow + 1}`).format = {
      fill: COLORS.blueSoft, font: { name: "Microsoft YaHei", size: 10, color: "#415466" }, rowHeight: 34, wrapText: true,
    };
    const rateRow = entry.rows.findIndex((row) => row[0] === "已观察会话新开比例") + 1;
    sheet.getRange(`B${rateRow}`).format.numberFormat = "0.0%";
    sheet.getRange(`D${rateRow}`).format.numberFormat = "0.0%";
    const queryHeader = entry.rows.findIndex((row) => row[0] === "触发词") + 1;
    const queryEnd = queryHeader + analysis.fallbackReopen.byQuery.length;
    sheet.getRange(`F${queryHeader + 1}:G${queryEnd}`).format.numberFormat = "0.0%";
    const distributionTitleRow = entry.rows.findIndex((row) => row[0] === "下一会话时间分布（会话级）") + 1;
    sheet.getRange(`A${distributionTitleRow}:G${distributionTitleRow}`).merge();
    sheet.getRange(`A${distributionTitleRow}:G${distributionTitleRow}`).format = {
      fill: COLORS.tealSoft, font: { name: "Microsoft YaHei", size: 12, bold: true, color: COLORS.teal }, rowHeight: 30,
    };
    sheet.getRange(`A${distributionTitleRow + 1}:G${distributionTitleRow + 1}`).merge();
    sheet.getRange(`A${distributionTitleRow + 1}:G${distributionTitleRow + 1}`).format = {
      fill: COLORS.blueSoft, font: { name: "Microsoft YaHei", size: 10, color: "#415466" }, rowHeight: 34, wrapText: true,
    };
    const delayHeader = entry.rows.findIndex((row) => row[0] === "新开间隔") + 1;
    const delayEnd = delayHeader + analysis.fallbackReopen.delayBands.length;
    sheet.getRange(`E${delayHeader + 1}:F${delayEnd}`).format.numberFormat = "0.0%";
    const timedBandEnd = delayHeader + analysis.fallbackReopen.delayBands.length - 1;
    const delayChart = sheet.charts.add("column", sheet.getRange(`A${delayHeader}:C${timedBandEnd}`));
    delayChart.title = "对 vs 嗯：下一会话时间分布";
    delayChart.hasLegend = true;
    delayChart.setPosition(`I${delayHeader}`, `P${delayHeader + 16}`);
  }
}

const opening = sheets.get("新版开场分析");
if (report.primary.openingByDifficulty.length) {
  const end = 4 + report.primary.openingByDifficulty.length;
  opening.sheet.getRange(`D5:D${end}`).format.numberFormat = "0.0%";
  const chart = opening.sheet.charts.add("bar", opening.sheet.getRange(`A4:D${end}`));
  chart.title = "不同理解难度的原始记录接续";
  chart.hasLegend = true;
  chart.setPosition("K4", "R17");
}
if (report.primary.openingByType.length) {
  const start = 7 + report.primary.openingByDifficulty.length;
  opening.sheet.getRange(`D${start}:D${start + report.primary.openingByType.length - 1}`).format.numberFormat = "0.0%";
}

const subIntent = sheets.get("有效对话子意图对比").sheet;
const subIntentEnd = 4 + report.primary.subIntentComparison.rows.length;
if (subIntentEnd >= 5) {
  subIntent.getRange(`C5:C${subIntentEnd}`).format.numberFormat = "0.0%";
  subIntent.getRange(`E5:F${subIntentEnd}`).format.numberFormat = "0.0%";
}

const intent = sheets.get("真实意图分析");
const intentHeader = 6 + report.primary.intentAnalysis.status.length;
if (report.primary.intentAnalysis.status.length > 0) {
  intent.sheet.getRange(`C5:C${4 + report.primary.intentAnalysis.status.length}`).format.numberFormat = "0.0%";
}
const intentCount = Math.max(report.primary.intentAnalysis.main.length, report.primary.intentAnalysis.sub.length);
if (intentCount > 0) {
  intent.sheet.getRange(`C${intentHeader + 1}:C${intentHeader + intentCount}`).format.numberFormat = "0.0%";
  intent.sheet.getRange(`F${intentHeader + 1}:F${intentHeader + intentCount}`).format.numberFormat = "0.0%";
}
const intentEnd = intentHeader + Math.min(10, report.primary.intentAnalysis.main.length);
if (intentEnd > intentHeader) {
  const chart = intent.sheet.charts.add("bar", intent.sheet.getRange(`A${intentHeader}:B${intentEnd}`));
  chart.title = "真实主意图 Top 10";
  chart.hasLegend = false;
  chart.setPosition("H4", "O18");
}

if (report.primary.demographicAnalysis) {
  const demographic = sheets.get("年龄性别概览").sheet;
  demographic.getRange("F4").format.numberFormat = "0.0%";
  const statusEnd = 6 + report.primary.demographicAnalysis.status.length;
  const distributionStart = statusEnd + 3;
  const distributionEnd = distributionStart + report.primary.demographicAnalysis.distribution.length - 1;
  if (distributionEnd >= distributionStart) {
    demographic.getRange(`F${distributionStart}:F${distributionEnd}`).format.numberFormat = "0.0";
    demographic.getRange(`H${distributionStart}:H${distributionEnd}`).format.numberFormat = "0.0%";
  }
  const chart = demographic.charts.add("bar", demographic.getRange(`A6:B${statusEnd}`));
  chart.title = "人口属性解析状态";
  chart.hasLegend = false;
  chart.setPosition("K4", "R17");
}

for (const [name, analysis, title, limit] of [
  ["所在地分析", report.primary.locationAnalysis, "所在地用户数 Top 20", 20],
  ["星座分析", report.primary.constellationAnalysis, "星座用户分布", 12],
]) {
  if (!analysis) continue;
  const entry = sheets.get(name);
  entry.sheet.getRange("F4").format.numberFormat = "0.0%";
  const statusEnd = 6 + analysis.status.length;
  const distributionHeader = statusEnd + 2;
  const distributionStart = distributionHeader + 1;
  const distributionEnd = distributionStart + analysis.distribution.length - 1;
  if (distributionEnd >= distributionStart) {
    entry.sheet.getRange(`C${distributionStart}:C${distributionEnd}`).format.numberFormat = "0.0%";
    entry.sheet.getRange(`F${distributionStart}:F${distributionEnd}`).format.numberFormat = "0.0";
    entry.sheet.getRange(`H${distributionStart}:H${distributionEnd}`).format.numberFormat = "0.0%";
    const chartEnd = Math.min(distributionEnd, distributionHeader + limit);
    const chart = entry.sheet.charts.add("bar", entry.sheet.getRange(`A${distributionHeader}:B${chartEnd}`));
    chart.title = title;
    chart.hasLegend = false;
    chart.setPosition("K4", "R20");
    addTable(entry.sheet, `A${distributionHeader}:I${distributionEnd}`, name === "所在地分析" ? "LocationAnalysisTable" : "ConstellationAnalysisTable", "TableStyleMedium2");
  }
}

const issueOverview = sheets.get("新版问题总览");
if (issueOverview.rows.length > 4) {
  issueOverview.sheet.getRange(`G5:G${issueOverview.rows.length}`).format.numberFormat = "0.0%";
}

const openingDetail = sheets.get("新版开场明细");
if (openingDetail.rows.length > 1) addTable(openingDetail.sheet, `A1:L${openingDetail.rows.length}`, "OpeningDetailTable");
const validEndingDetail = sheets.get("五轮以上结束明细");
if (validEndingDetail.rows.length > 1) addTable(validEndingDetail.sheet, `A1:${columnName(validEndingDetail.rows[0].length)}${validEndingDetail.rows.length}`, "ValidEndingDetailTable");
const rawEndingDetail = sheets.get("五轮以上结束明细（不剔除无效）");
if (rawEndingDetail.rows.length > 1) addTable(rawEndingDetail.sheet, `A1:${columnName(rawEndingDetail.rows[0].length)}${rawEndingDetail.rows.length}`, "RawEndingDetailTable", "TableStyleMedium7");
for (const name of detailNames) sheets.get(name).sheet.freezePanes.freezeRows(1);
for (const [name, tableName, style] of [["需关注用户汇总", "AttentionUsersTable", "TableStyleMedium9"], ["用户风险表达明细", "UserRiskExpressionsTable", "TableStyleMedium3"]]) {
  const entry = sheets.get(name);
  addTable(entry.sheet, `A1:${columnName(entry.rows[0].length)}${entry.rows.length}`, tableName, style);
}
for (const [name, start, style] of [["新版问题总览", 4, "TableStyleMedium9"], ["新版质量问题", 4, "TableStyleMedium9"], ["新版安全问题", 4, "TableStyleMedium3"]]) {
  const entry = sheets.get(name);
  if (entry.rows.length > start) addTable(entry.sheet, `A${start}:${columnName(entry.rows[0].length)}${entry.rows.length}`, `${name === "新版问题总览" ? "IssueOverview" : name === "新版质量问题" ? "QualityIssues" : "SafetyIssues"}Table`, style);
}

const outputPath = path.resolve(options["--output"]);
await fs.mkdir(path.dirname(outputPath), { recursive: true });
const inspectWorkbook = workbook.inspect.bind(workbook);
workbook.inspect = () => inspectWorkbook({ kind: "workbook,sheet", maxChars: 1_000_000 });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
await freezeDetailHeaders(outputPath, report.sheetOrder, detailNames);
console.log(JSON.stringify({ outputPath, sheets: report.sheetOrder.length }));

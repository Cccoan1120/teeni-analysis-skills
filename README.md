# Teeni 分析 Skill

这个开源仓库包含两个 Codex Skill、本地兴趣分析引擎，以及向兼容的 Teeni 看板发布结果所需的后端代码。仓库不包含真实对话、分析结果、数据库、密钥或线上地址。原始数据和工作产物都应放在仓库外。代码采用 MIT 许可证。

## 安装

需要 Windows PowerShell、Python 3.11 及以上版本和 Node.js 20 及以上版本。基础分析生成与验证 Excel 时还需 Codex 工作区运行时提供的 `@oai/artifact-tool`；此包没有放进仓库。通过 Codex 的 `load_workspace_dependencies` 找到其 `node_modules` 目录，并设置 `TEENI_NODE_MODULES`。缺少该依赖时，基础分析不能完成工作簿生成和验证。

在克隆后的仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:TEENI_ANALYSIS_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
$env:TEENI_NODE_MODULES = '<Codex 运行时的 node_modules 目录>'
```

在此仓库打开 Codex 时，`.agents/skills/` 下的两个 Skill 会被发现。原始 CSV 分析调用 `$analyze-teeni-conversations`，已验证基础明细的兴趣分析调用 `$analyze-teeni-interests`。基础 Skill 的 `scripts/`、`references/`、`vendor/` 和 UI 元数据均在仓库内；兴趣引擎及实体库位于 `interest_engine/`。

## 基础分析与发布

由看板管理员通过受保护的本地环境提供发布 API 地址和令牌。地址应指向发布接口，不是网页首页。不要把令牌写进仓库或命令参数。

```powershell
$env:TEENI_DASHBOARD_PUBLISH_URL = '<获授权的看板发布 API 地址>'
$env:TEENI_PUBLISH_TOKEN = '<管理员提供的令牌>'
.\scripts\analyze-and-publish-dashboard.ps1 -Csv '<本地原始 CSV 路径>' -DataDate 'YYYY-MM-DD' -ProductVersion M1 -OutputDir '<仓库外的输出目录>'
```

`M1` 对应 scene 488，`M2` 对应 scene 904。`DataDate` 必须是已确认的业务日期。联动命令逐行检查日期和场景，完成基础分析、独立验证与开场群体聚合后，才向看板发送聚合快照；原始 CSV 和逐轮明细不上传。取得匹配日期及工作簿哈希的回执后，还要核对目标看板的快照与健康状态，才能报告线上已更新。

仅在有意重试一个已验证的既有包时，使用 `scripts/publish-dashboard.ps1`，显式传入 `-Workbook`、`-Manifest`、`-PrimarySource` 和 `-DataDate`。若上次响应不明，先回读目标状态。不要用既有包测试真实看板发布。

## 兴趣分析与发布

使用同产品、同日期的已验证基础明细 CSV 与清单。默认实体库是 `interest_engine/resources/entity-registry.db-v3.json`，含 232 个已审核实体，SHA-256 为 `6c1f0d701146f73cb16d222ff60c45d003896e940debd05e2085fd57a069d3f2`，与 2026-09-23 兴趣清单使用的实体库一致。单日 CLI 默认使用此文件；批量分析须在仓库外创建 `teeni-interest-batch/1.0.0` 清单，显式指向该文件或相同哈希的私有副本，并按[兴趣 Skill](.agents/skills/analyze-teeni-interests/SKILL.md)记录实体库、基础明细和基础清单的 SHA-256。从仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m interest_engine.batch_cli analyze --manifest '<本地 batch.json>' --output-dir '<新的私有输出目录>' --cache-dir '<私有缓存目录>'
.\.venv\Scripts\python.exe -m interest_engine.batch_cli verify --manifest '<本地 batch.json>' --result-manifest '<输出目录>\teeni-interest-batch-result.json'
```

完成真实的候选审核后，才可在私有 batch 清单中将 `candidateReviewComplete` 改为 `true`，再用最终实体库重新分析和验证。对应日期、产品的基础快照应先发布到目标看板。

管理员通过受保护环境提供 `TEENI_INTEREST_IDENTITY_KEY`。本地制包环境和目标服务器必须使用同一获授权密钥，且跨日期保持稳定。然后在本地准备签名聚合包：

```powershell
$env:TEENI_INTEREST_IDENTITY_KEY = '<管理员提供的身份密钥>'
.\.venv\Scripts\python.exe manage.py prepare_interest_aggregates --manifest '<已审核 batch.json>' --result-manifest '<已验证 result.json>' --output-dir '<新的私有聚合包目录>'
```

只通过获授权的私有通道传输每个日期的签名包。包内 `contribution.private.json` 含私有伪名化贡献，不能成为公开下载。目标服务器需要运行兼容的看板代码，并先加载其受保护环境，然后执行：

```text
python manage.py import_interest_aggregates --package <私有聚合包/日期/package.json>
python manage.py import_interest_aggregates --package <私有聚合包/日期/package.json> --apply --backup-dir <新的私有备份目录>
```

第一条命令只校验，第二条按产品和日期写入并要求新的范围备份。完成后核对导入回执、目标快照、累计覆盖和看板健康状态。克隆仓库不会自动获得服务器权限或密钥，这些由管理员另行分配。

## 本地检查

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe -m unittest interest_engine.tests.test_batch interest_engine.tests.test_cli
.\.venv\Scripts\python.exe manage.py test interests.tests.test_import_interest_aggregates publisher.tests.test_cli
```

基础 Skill 的匿名回归入口为 `.agents/skills/analyze-teeni-conversations/scripts/self-test.mjs`，运行前需设置 `TEENI_ANALYSIS_PYTHON` 和 `TEENI_NODE_MODULES`。发布代码只能用 `--output`、自动化测试或一次性测试数据库检查，不能向真实看板数据库发送测试包。

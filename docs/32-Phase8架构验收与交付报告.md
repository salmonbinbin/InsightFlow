> **最新修复与复验（2026-10-17，v5）：Phase8 PARTIAL，Research质量FAIL，未验收、未提交、未推送。** 已实现保守任务/证据覆盖、官方正文、证据ID引用及安全诊断；最终SQLite1092/11skip、隔离PG1103与迁移往返、Ruff、前端154及lint/build通过。原十题为2完整/6局部/2未验证，复杂问题仍未闭合。详见[39号修复与独立验收报告](39-Phase8任务覆盖修复与独立验收报告.md)。以下保留旧阶段快照；36号根因更正仍有效。STOP，不进入Phase9。

> **最新交接（2026-10-17，深度根因分析）：Phase 8 PARTIAL，Research质量FAIL，未验收、未提交、未推送。** 现有完成门槛不验证整题覆盖，检索/引文表示/软降级纠错另有断点；本轮只做分析与交接，没有新产品修复或真实模型验收。请先读[36号根因与新对话交接报告](36-Phase8Research根因与新对话交接报告.md)及[37号启动Prompt](37-Phase8新对话启动Prompt.md)。36号报告已更正旧Dropbox overnight归因、iCloud缺risks分类和跨行引文的笼统解释；以下为历史记录。Phase8后STOP，不进入Phase9。

> **最新（2026-10-17，v4）：Phase 8 PARTIAL / 未验收 / 未提交 / 未推送。** 最终SQLite972/11skip、隔离PostgreSQL983与迁移往返、Ruff、前端147及lint/build通过；真实十条为5completed/5partial_failed，官方全文核对仅2完整回答、3局部事实、5未验证，Research质量FAIL。详见[本轮分层定位与复验](35-Phase8Research分层定位与本轮复验.md)。STOP，不进入Phase9。下文旧状态为历史记录。

# Phase 8 架构验收与交付报告（2026-10-16）

负责人：三文鱼。执行：Codex Supervisor → Claude Code Worker。**状态：PARTIAL；未验收、未提交、未推送。** 最新复验见文末“最终版补充验收”；前文各轮数字保留为历史快照。本阶段 STOP，不进入 Phase 9。

## 范围与架构

- Research Agent：Insight 派生有限内部上下文，Tavily 返回公开检索来源，MiniMax-M3 通过版本化 `research-agent-v2` 结构输出；`research_runs`、`research_sources`、`research_findings` 与迁移 0012 保存独立审计。Internal Evidence 与 External Research 分表、分区展示。Run 完成至少需要一条带本次来源的竞品或市场发现；来源 ID/URL、输出 Schema 及状态严格校验。最近增加产品短语过滤、低权威来源过滤、竞品名称与引用来源文本匹配守卫。它们是保守的自动校验，**不能证明整段发现的语义得到来源支持**。
- Tool Layer：静态注册表、`tool_runs`、`simulated_issues` 与迁移 0013。`simulated_issue_tracker.create_issue` 只在本地模拟；Prepare 冻结服务器派生的 Title/Description/Priority/Source Insight，Cancel 不执行，Approve 再核对 Requirement 仍为 `approved`，随后写入 `SIM-...`。`project_id + request_id` 与 Requirement/工具动作绑定；PostgreSQL 行锁及状态条件更新约束并发 Prepare、Approve、Cancel。前端在 Requirement 详情展示冻结预览和“仅模拟”标记。
- **Deferred**：真实 Jira、Notion、飞书写入和 MCP 未接入；`SIM-...` 不是外部工单。通用多 Agent 编排、Phase 9 也不在本次范围。

## Codex 独立验收证据

| 门槛 | 本轮结果 | 说明 |
| --- | --- | --- |
| Tool PostgreSQL 事务专项 | **PASS**：最终独立专项 72 passed，新增撤销批准与 Approve 竞态回归 | 并发双 Approve 仅一次 adapter 调用、Approve/Cancel 竞争、并发相同 request ID Prepare；撤销批准 409、取消无结果、重复批准幂等、timeout/5xx 审计在专项覆盖。随机 schema 隔离测试验证建表及 ORM 读写；并发线程起点同步不保证穷举所有调度。 |
| 后端 SQLite 全量 / Ruff | **PASS**：最终 846 passed、11 skipped；`ruff check app tests alembic scripts` 通过；Research 专项 157 passed | 11 项跳过含 PostgreSQL 专项；不代表真实 Research 质量。 |
| 全新隔离 PostgreSQL 全量和迁移 | **PASS**：PostgreSQL 16.2，857 passed；`alembic upgrade head → check → downgrade base → upgrade head → check`，`POSTGRES_VERIFICATION_PASSED` | 测试脚本只操作全新临时数据库。迁移 0012/0013 在完整 head 链内。 |
| 前端 | **PASS**：lint 无警告，Vitest 144 passed / 15 files，生产构建 11 路由 | 单元与构建不能代替浏览器。 |
| 真实浏览器链路 | **工程流程 PASS，Research 语义 FAIL**：隔离 PostgreSQL + FastAPI `127.0.0.1:18080` + Next.js `127.0.0.1:13080` + 真实 Tavily/MiniMax | 详见下节；测试服务与用户库分离。 |
| 真实 Research 人工质量 | **FAIL**：本次又复验两轮各 5 条；最新轮 1 completed、4 partial_failed，唯一 completed 仍含来源未支持的断言，人工可交付 0/5 | 不能提交；详情见下方续验。 |

最终 Tool 测试夹具只在新建的随机 schema 内建表和清理。独立审查发现上一版新增测试含 `DROP TABLE public.tool_runs CASCADE`，已在执行 PostgreSQL 测试前删除；随后用实际失败栈修复扩展类型解析、测试种子和重复 fixture。最后一版重新通过上述全量检查。以上工程结果与 Research 人工质量判定分别记录。

## 真实 Research 抽样与人工核对

同一隔离测试 Insight，用五个具体产品问题分别运行；每轮均用全新临时 PostgreSQL、真实 Tavily、真实 MiniMax-M3。原始评测 JSON 仅留在本机 `/tmp/insightflow_phase8_real_research_eval_v3.json` 与 `..._v4.json`，路径可能被系统清理；以下是脱敏后的持久摘要，不含密钥或 Raw Feedback。

| 对象 | v3 状态 | 最终 v4 状态与 Run ID | 人工核对 |
| --- | --- | --- | --- |
| Google Photos | completed | `partial_failed` · `b6c9c7b2-c571-4fa8-b5b1-70774614473a`；0 sources、3 findings | v3 主要引用非官方 Google Sites 资料且无法证明所问的移动端上传限制。v4 因无可用来源保持未证实，守卫正确，但无可交付结论。 |
| Dropbox | `partial_failed`（额外顶层字段） | `partial_failed` · `8bcba795-8c8d-4b5e-a18d-71e4580e0c48`；1 source、4 findings | 唯一来源为社区讨论；只保存 `not_verified` 推断，没有官方帮助支持的竞品结论。 |
| OneDrive | `partial_failed`（额外顶层字段） | `partial_failed` · `651fee99-f8d5-489f-a8c0-10b65bbd77cf`；5 sources、4 findings | 虽检索到 Microsoft Support，未生成带来源的可确认竞品发现。 |
| iCloud Photos | `partial_failed`（缺少 tool_use） | `completed` · `23656352-5cdb-4227-8b2b-97da96416664`；5 sources、5 findings | 模型把 iCloud.com 网页上传和共享图库流程概括为 iPhone 本机上传/多选能力；所引 Apple 页面不能充分支持这个合并结论，人工质量不通过。 |
| Google Drive | completed | `completed` · `8070c888-63f8-4975-baf1-49eb404c8d32`；5 sources、5 findings | v3 曾把 iCloud Photos 列为 confirmed 并引用 Google Drive 社区帖；v4 已避免该错，但仍主要依赖社区帖、论坛、视频，未满足问题中“官方 Google 帮助”的来源要求。 |

v4 的接口完成率是 **2/5**，人工可交付竞品结论率是 **0/5**；两轮 10 次调用里也未达到本阶段所需的稳定、来源支持质量。`completed` 只说明当前程序守卫通过。链接存在、来源 ID 合法、产品名出现在片段里，与“来源支持整段事实及限制”是四种不同检查。搜索结果或页面可能变化，复测须重跑并逐条阅读来源。

### 本次续验：并发竞态、Research 修复与两轮真实抽样

Codex 在全新隔离 PostgreSQL 复现“Approve 已读到 approved → adapter 前暂停 → 另一事务把 Requirement 改为 draft 并提交 → 释放后仍创建模拟结果”。原 Tool Claude `threadId=520a4b77-2518-4ca2-9fc9-439b21245afb` 的修复 jobId 为 `705a89ad650944faa83680ad78c07b05`、`73b9235182f04ff49c058f61e571a54d`：批准路径在 PostgreSQL 锁定 Requirement，并新增两条并发回归。首轮独立测试为 71 passed、1 failed（测试误导入不存在的类），续修后全新隔离 PostgreSQL Tool 专项 72 passed。取消后零模拟写入、重复批准同一 external ID、request ID 绑定、provider timeout/5xx 的既有回归随全量复验通过。

Codex 用真实返回复现来源过滤缺陷：Google Drive 查询的短语列表含 `Google Drive Help / Mobile / Cite`，Google Photos 来源被泛词错误放行。原 Research Claude `threadId=8d048dd4-fb66-4838-898b-44f10cedd34c` 在 job `45d30838eea24620a456fe61afdac592`、`386917cc312948d0822c05b4e6981811`、`96d1759487d642f09db6722434ab9797`、`640d99f1f5c644b897219cf67d235a91` 中限定修改服务和测试；Codex 连续发现 4 项回归、测试 fixture 自相矛盾、过滤停用词大小写不匹配，逐次提供失败证据续修。最终 Research 专项 **157 passed**，Schema、source_refs 与 grounding 守卫没有放松。来源过滤仍是词面判断，不能代替事实核查。

续验 v5 与 v6 都是各 5 条、全新临时 PostgreSQL、真实 Tavily 和真实 MiniMax-M3；原始样本仅留 `/tmp/insightflow_phase8_human_eval_v5.json` 与 `..._v6.json`，可能被系统清理。以下为最终 v6 的脱敏持久结论：

| 对象 | v6 Run ID / 程序状态 | Codex 来源核对 |
| --- | --- | --- |
| Google Photos | `0623e730-79b9-4680-8b95-2c9fcae4deef` · `partial_failed` | 5 来源、0 findings；模型重试后仍有额外顶层字段。虽有 Google 官方帮助页，未形成可交付发现。 |
| Dropbox | `6645fed7-ee6a-406e-92f1-adf4e589b9a7` · `partial_failed` | 5 来源、6 findings，均无带官方帮助引用的竞品事实；社区帖、第三方文章和视频不能满足“仅官方帮助”。 |
| OneDrive | `58fde6e6-92bd-4650-9858-a11eab79d3da` · `partial_failed` | 5 来源、0 findings；Microsoft 官方手动上传页已在结果中，但模型仍输出额外顶层字段。官方页原文可支持“选择图片并点 Done”，本 Run 没有可靠地结构化该事实。 |
| iCloud Photos | `2876bd88-a76d-4574-abbc-91d3dec87e22` · `completed` | 5 来源、8 findings。模型称 iCloud.com 手机上传“没有原生多选手势”、Shared Library 可经 Photos 的 Select 手势批量移动；所引苹果页面只说明手机可选 Photo Library / Choose Files，以及共享图库应使用 Photos App，**没有证明这些否定或批量移动断言**。完成状态是误阳性，人工质量 FAIL。 |
| Google Drive | `e0eda1cc-e33d-4650-bd1a-9285ee6dbaac` · `partial_failed` | 5 来源、3 findings；主要是社区帖、第三方和视频，缺少满足“仅官方 Google Drive Help”且直接证明 Android 多图一次上传的页面。保持未证实正确。 |

v5 为 **0 completed / 5 partial_failed**；v6 为 **1 completed / 4 partial_failed**；两轮人工可交付均为 **0/5**。`unknown_top_level_field`、缺少官方来源和“来源没说不存在却推断不存在”是不同失败。v6 的 iCloud 误阳性说明当前自动守卫仍不能保证语义真实性，本阶段 Research 质量门槛 **FAIL**。

## 真实 FastAPI + 浏览器流程

隔离项目 A `b8c94de4-7345-487e-b880-5e208dec50b5`、项目 B `3fdd5d1e-d041-42ea-a3b9-a3390b681c11`；研究 Insight `cdd1ebbf-a8c1-4578-ae5c-b907f5bc21f9`、批准 Requirement `a032bd94-9e43-4f2c-bd12-1ed9849c5bd6`。这些 UUID 属于临时库，服务退出即不可复用。

1. 真实浏览器打开 Insight 详情，Internal Evidence 与 External Research 独立显示；输入 OneDrive 问题点击 Run Research，页面从 FastAPI 读回 Run `5d2ecbb7...`，状态 `Completed`，7 findings、5 sources、模型 `MiniMax-M3`、Prompt `research-agent-v2`、延迟约 13.8 秒。页面展开竞品发现及 Sources。此流程证明调用和持久化可用，**不证明该 Run 的结论语义正确**。
2. 在同一页面切换空项目 B，页面显示“该洞察不属于当前项目”；Research 历史请求带 B 的 `project_id` 返回 404，A 的内容不继续展示。浏览器控制台另有 favicon 404 及不存在的 Opportunity Score 404，未影响上述路径。
3. 打开项目 A 的批准 Requirement，Prepare 展示 `[Simulated] 多选模式提示气泡`、描述、`P1` 和源 Insight；Cancel 后显示“已取消；无模拟结果”。重新 Prepare 后 Approve，显示 `SIM-699536b3e1cd475b8da26cb5` 与 `simulated`，明确“未创建任何真实外部对象”。History 中一个 cancelled Run 无 external ID、一个 completed Run 有 `SIM-...`。对 completed Run 再发一次 Approve 返回 200 且 external ID 不变。

上述浏览器流程使用测试种子数据，不把合成内部 Insight 当作真实用户洞察，也不把模拟 Tool 当作真实外部系统。完整从 CSV → AI 分类 → 评分 → Requirement/PRD 的本轮新建数据流程 **NOT MEASURED**；Phase 8 页面的独立接线和审批路径已实测。

最终前端构建后，原先保持运行的 Next.js 开发服务曾对详情页返回 500。Codex 停止该临时服务并重新启动全新隔离数据库、FastAPI 和 Next.js 后，首页、Insight 详情、Requirement 详情均返回 HTTP 200；重启后的服务未重复完整点击流程。此前浏览器交互证据对应同一版应用代码，最后一轮仅修改 Tool 测试夹具和交付文档。

本次重新在真实浏览器执行隔离联调。首个临时库项目 A `5886d727-8448-42e2-84c5-52fd85f9bab7`、B `0d19a609-165c-457a-940c-f1976b4acd74`、Requirement `0c453029-bed3-47ca-8eef-6384aa50e831`：浏览器 Prepare → Cancel 后无模拟结果；重新 Prepare → Approve 后显示 `SIM-1f0d61d147984d45a192c8d7` 和 simulated 标记。API 历史恰有一条 cancelled（无 external ID）与一条 completed；重复批准 completed Run 返回 200、ID 不变；项目 B 读该 Requirement 历史为 404。切换到 B 后页面隐藏 A 的 Requirement 和 Insight。

Research 修复后重新启动另一全新隔离库，项目 A `341a20d6-2da5-421c-9163-513c8cdb92f0`、B `17f156b9-7bf1-49b7-ba25-f2cbe5c8151e`、Insight `d0bc13b8-fb8d-438e-823a-dceb9ed7d8e4`：浏览器输入 OneDrive 官方来源问题并点击 Run Research，FastAPI 真实调用 Tavily/MiniMax 后落库 Run `f2b3f6da-fb36-473d-b2ee-9233597742aa`；页面显示 `Partial Failed`、5 Sources、0 Findings 与 `unknown_top_level_field`，没有伪装成已证实结论。API 的 Run/Sources/Findings 分别返回 200/200/200（来源 5、发现 0）；带 B 的 `project_id` 访问三者均为 404。两轮浏览器服务和 18080/13080 端口已停止。以上证明流程和隔离边界，不改变真实 Research 质量 FAIL。

## Gate、限制与 Git

- 自动测试、数据库迁移、模拟审批并发和页面联调均有本轮证据；**真实 Research 人工质量门槛失败**，Phase 8 因此维持 `PARTIAL`。
- Phase 2 真语义 Embedding、Phase 3 人工语义复核、Phase 4 评分业务质量、Phase 6 未测指标、Phase 7 模型稳定性及人工产品验收保留历史限制，不能由本轮工程测试改写为 PASS。
- 当前 Phase 8 文件和本报告仍在未提交工作树；两份用户原型 ZIP、Raw Feedback、`key/.env` 与临时数据库未纳入提交。**未 commit，未 push。** 本阶段停止，等待三文鱼人工验收或授权本阶段质量返工；不推进 Phase 9。

人工逐步复测见 [Phase 8 人工复测流程](33-Phase8人工复测流程.md)。

## v13 补充验收（历史快照）：引文误阳性修复与十条真实抽样

Codex 对 v10a/v10b 的完成项逐字核对后发现：Google Drive 官方句子“Find and tap the files you want to upload.”既不能证明“一次多选”，也不能证明“只能逐文件点选”；另一句“Touch and hold the item.”不能证明“避免多选的替代流程”。原 Research Claude 会话 `8d048dd4-fb66-4838-898b-44f10cedd34c` 接受具体失败证据，续修 jobId 为 `ce0cb8d50af54cd4ade7c78afa880e0e`（600 秒超时，独立专项 245 passed / 8 failed）、`d5b2f620d1d2441ba6e1823c6ab48048`（253 passed，但 v11a 真实调用仍有 Google Drive 误阳性）、`0b2965ad76734876be8509e9d4a1aa75`（256 passed，但 v12a 五条真实调用全部降级）、`5adae484caa84001aa18dcfdbd7dee51`（最终专项 263 passed）。每轮均检查实际代码、独立复测后才继续；Claude 的完成声明没有作为验收依据。

最终守卫要求 confirmed 竞品/市场发现使用来源片段中的逐字短引文，描述只能有固定的无事实扩写前缀；“多选/批量”等明确数量问题还需引文本身包含明确数量词。来源 ID、URL、官方来源要求、引用片段连续匹配和完成门槛仍有效。这是针对已见误阳性的保守规则，**不能证明所有开放研究问题的语义蕴含**；标题、分类及未证实的推断仍需人工来源核对。

最终两轮各五条均使用新建的临时 PostgreSQL、真实 Tavily 与真实 MiniMax-M3。原始 JSON 暂存于 `/tmp/insightflow_phase8_human_eval_v13a.json`、`..._v13b.json`，以下摘要不含密钥和 Raw Feedback：

| 产品 | v13a Run / 状态 | v13b Run / 状态 | Codex 对照官方原文的判断 |
| --- | --- | --- | --- |
| Google Photos | `503b1d3a-436e-4726-9bd4-6b7626d7bf33` · `partial_failed` | `7b2427b7-5cea-4df9-b7e3-6c33a5591996` · `partial_failed` | 两次均没有带有效来源的确认发现；不能交付多图手动备份结论。 |
| Dropbox | `37fb368f-5644-4f7e-b3a9-8e104dc1b596` · `partial_failed` | `a7709655-4f66-4390-a9b5-4d1cdd57791e` · `partial_failed` | 两次均未形成官方来源支持的确认发现。 |
| OneDrive | `3dc3523d-1e22-462f-9b85-f88460e550fb` · `completed` | `f9242306-ac30-49fa-a540-6f04232cdade` · `completed` | Microsoft Support 原文明确有“Select one photo first, then use two fingers to slide to select multiple files or photos”；手动上传页还写明选择图片后点 Done。当前完成项的窄事实有来源支持；不扩写为所有版本、所有入口均一致。 |
| iCloud Photos | `f5934ef2-4d3c-4f18-8797-fcda88385b5b` · `partial_failed` | `d2ca60a6-c175-45de-933a-8a8db8c2e9df` · `partial_failed` | 没有可确认的目标操作结论；保留 Photos App、iCloud.com 与 Shared Library 的平台区分。 |
| Google Drive | `009f8832-7da0-4a35-9cc4-367881a9d46b` · `partial_failed` | `6e2ea3f6-99f3-423f-8c82-d74e59ce1660` · `completed` | v13a 的“是否一次多选”保持未证实；v13b 问的是一般 Android 上传步骤，Google Drive Help 原文逐项支持打开 App、New→Upload、点选文件及在 My Drive 查看。**v13b 的完成状态不能反推 v13a 的多选问题已获回答。** |

十条真实调用中 **3 completed、7 partial_failed**。Codex 逐条看完完成项的引用片段，三个完成项所显示的窄事实均有对应官方原文；但多数目标问题仍无可交付结论，且此样本量不能证明开放问题的长期准确率。因此“自动校验通过”“真实调用完成”“人工来源核对通过”分别记录，Research 整体质量门槛仍为 **FAIL**，Phase 8 保持 `PARTIAL`。

最终改动后 Codex 重新执行：Research 专项 **263 passed**；后端 SQLite 全量 **952 passed、11 skipped**；全新隔离 PostgreSQL 16.2 全量 **963 passed**，`alembic upgrade head → check → downgrade base → upgrade head → check` 和 `POSTGRES_VERIFICATION_PASSED`；Ruff `app tests alembic scripts` 通过。前端 lint 无警告，Vitest **144 passed / 15 files**，构建 **11 路由**。跳过项属于 PostgreSQL 专项；本段数字对应最终代码，不用前文历史快照替代。

最终代码再次运行隔离 FastAPI `127.0.0.1:18080` + Next.js `127.0.0.1:13080` 浏览器：项目 A `d565952e-16f3-4736-9539-13aa9cdb5943`、B `ab757633-3fb1-44f0-ad50-6b92da3fabf8`。Insight `0d03c3f0-f92e-4c5d-b594-11535d94e41a` 在页面分别显示 Internal Evidence 与 External Research，真实 OneDrive Research 完成且展示 Microsoft 引文和 5 个 Sources。Requirement `34aba958-6f39-4bfc-adf3-699cfe21bb08` 的 Prepare 冻结 Title/Description/Priority/Source Insight；Cancel 显示“无模拟结果”；重新 Prepare、Approve 显示 `SIM-96c3a00f612645969493b6be` 与 `simulated`。API 历史为一条 cancelled（无 external ID）和一条 completed（同一 `SIM-...`）；再次批准 completed Run 返回相同 ID；项目 B 读其历史返回 404，页面切换后也拒绝展示项目 A Requirement。浏览器控制台的 favicon 404 及空 Opportunity Score 404 未改变这些结果。临时浏览器、服务和数据库均已关闭。

Git 交付门槛未满足：**未本地提交，未 push，未进入 Phase 9**。真实 Jira、Notion、飞书写入及 MCP 仍是 deferred，`SIM-...` 仅为模拟。历史 Phase 2/3/4/6/7 质量限制保持原结论。

## 2026-10-17 最终版 v14 独立复验

Codex 继续审查发现 v13 描述守卫中的停用词回退可让 `No support. Evidence: "..."` 这类引文外否定句通过，尽管来源只支持引文。原 Research Claude 会话同一 `threadId=8d048dd4-fb66-4838-898b-44f10cedd34c` 的修复 jobId 为 `4f51273c19a5461aa0097678644ebb24`。最终实现只接受三种完整描述：纯 `Evidence: "逐字引文"`、`From the cited source. Evidence: "逐字引文"`、以及引用全部符合官方 allowlist 时的 `From the official help page. Evidence: "逐字引文"`。额外否定、尾随文本、第二个引文和改写前缀均降级；对应负例由 Codex 独立执行。此规则保证可展示描述只包含可回溯引文，仍**不能自动证明标题、分类及所有开放研究问题的语义正确**。

v14a/v14b 各五条均使用新建临时 PostgreSQL、真实 Tavily 与 MiniMax-M3；两轮 JSON 暂存 `/tmp/insightflow_phase8_human_eval_v14a.json` 与 `..._v14b.json`。Codex 对十条逐项核对，尤其重新检查所有 `completed` 的引用原文：

| 产品 | v14a Run / 状态 | v14b Run / 状态 | 独立来源判断 |
| --- | --- | --- | --- |
| Google Photos | `ca7f0301-fb62-4091-b06d-204aaa7038e2` · `partial_failed` | `5e3458d2-2b78-46c9-879a-360385c266c5` · `partial_failed` | 两次没有可交付的确认发现，不能回答 Android 多图手动备份。 |
| Dropbox | `8f37be12-6f8f-46de-8f86-3dfa9359bcfc` · `partial_failed` | `3fa7ca34-de32-497a-8001-3815980b96d2` · `partial_failed` | 两次没有官方来源支持的确认发现。 |
| OneDrive | `0bb2b5db-bda3-47c1-b1c8-b7eb3b90de3a` · `completed` | `8e15eb36-1fa3-4d5f-9390-5da23f1f2995` · `completed` | a 的 Microsoft Support 原文明确写“Select one photo first, then use two fingers to slide to select multiple files or photos”；b 的官方手动上传页明确写选择已有图片后点 Done。确认的窄事实有来源支持，不概括为所有版本和所有入口。 |
| iCloud Photos | `b0d31a38-c67b-45bd-86dc-d9031ab82db8` · `partial_failed` | `f8d51c18-a421-4389-9545-72424c3b7b4d` · `partial_failed` | 没有可确认目标操作结论，不能混同 Photos App、iCloud.com 与 Shared Library。 |
| Google Drive | `2927e516-1722-49f7-8b05-2a2089c5a6f4` · `partial_failed` | `83a3291e-727e-402f-9dcb-38c0a4e94587` · `partial_failed` | a 的“是否一次多选”没有官方原文支持；b 的普通上传步骤本轮也未形成合格确认发现。不能把先前 v13b 的一般上传步骤当作多选答案。 |

**最终真实样本为 2 completed / 8 partial_failed；两个完成项的窄事实经 Codex 对照官方原文通过，但总体 Research 可交付质量仍 FAIL。** 这是人工来源判断；它不同于自动测试 PASS，也不把 `partial_failed` 计为质量通过。Run ID 属临时数据库，评测进程退出后不可再查询；持久摘要保存在本报告。

最终代码独立回归：Research 专项 **267 passed**；后端 SQLite **956 passed、11 skipped**；全新隔离 PostgreSQL 16.2 **967 passed**，并完成 `alembic upgrade head → check → downgrade base → upgrade head → check`，输出 `POSTGRES_VERIFICATION_PASSED`；Ruff `app tests alembic scripts` 通过。前端文件未在 v13→v14 修改，最后一次前端 lint 无警告、Vitest **144 passed / 15 files**、生产构建 **11 路由**。Tool PostgreSQL 并发专项的 72 passed 及最终浏览器 Prepare/Cancel/Approve/重复批准证据见前文；Research 最终守卫之后又在全新隔离 FastAPI + Next.js 浏览器点击 OneDrive Research，页面显示 Run 前缀 `16a8c114` 为 `Completed`，展示上述 Microsoft 原文与 5 Sources。浏览器和临时服务均已关闭。

交付门槛结论：自动测试、隔离数据库迁移、模拟审批与浏览器接线通过；**Research 整体质量未过**，Phase 8 为 `PARTIAL / 未验收 / 未提交 / 未推送`。两份用户原型 ZIP、Raw Feedback 和密钥保留。`SIM-...` 仅是本地模拟，真实 Jira、Notion、飞书和 MCP 未接入。到此 STOP，不进入 Phase 9，等待三文鱼验收与后续决定。


## 2026-10-17 Phase 8 v4 最终独立复验：PARTIAL / STOP

Codex 续接原 Research 会话，修复官方查询丢 manual/mobile、Apple Support 品牌盖过 iCloud.com / Shared Photo Library，以及词形前缀误命中；新增独立 v4 Prompt，保留 v1—v3及 v14严格引文/数量/官方来源守卫。原前端会话只在 Research 终态增加“回答完整性：未验证”，不把 completed 当成完整答案。jobId：`46a27c79f3914bb59c69e8efe069c1e2`、`19900ffa7c8b4dcd83d4554eeadd2c8d`、`dc6b1408ebd745a0863488060f07d2af`；失败、独立红绿和保护检查详见新报告。

最终代码独立验证：Research **283 passed**；SQLite **972 passed / 11 skipped**；全新隔离 PostgreSQL **983 passed**，完整迁移 upgrade/check → downgrade base → upgrade/check 与 POSTGRES_VERIFICATION_PASSED；Ruff通过；前端 lint、**147 passed / 15 files**、生产构建通过。真实 FastAPI＋生产 Next.js 浏览器完成真实 Research、Sources、项目隔离和模拟 Prepare/Cancel/Approve；重复批准保持相同SIM ID，数据库仅一条模拟目标，Raw快照不变，临时服务已关闭。

启动环境检查发现：后端目录进程未继承Tavily，根目录启动可读取；没有要求用户发送或修改密钥。最终从根目录启动隔离进程，真实 Tavily＋MiniMax-M3 十条为 **5 completed / 5 partial_failed**；Codex对照官方全文判 **2完整回答 / 3局部事实 / 5未验证**，3个completed存在完整性误阳性。Google Photos官方有多图备份说明但检索片段遗漏；Google Drive有来源却仍出现模型tool_use失败。**Research质量FAIL，Phase8保持PARTIAL、未验收、未提交、未推送，STOP，不进入Phase9。** 历史Phase2/3/4/6/7限制和真实Jira/Notion/飞书/MCP deferred均保留。

逐条Run、原文判断、命令、截图与差异见 [本轮分层定位与复验](35-Phase8Research分层定位与本轮复验.md)。

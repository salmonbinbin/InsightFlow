> **最新（2026-10-17，v4）：Phase 8 PARTIAL / 未验收 / 未提交 / 未推送。** 最终SQLite972/11skip、隔离PostgreSQL983与迁移往返、Ruff、前端147及lint/build通过；真实十条为5completed/5partial_failed，官方全文核对仅2完整回答、3局部事实、5未验证，Research质量FAIL。详见[本轮分层定位与复验](35-Phase8Research分层定位与本轮复验.md)。STOP，不进入Phase9。下文旧状态为历史记录。

# Phase 8 测试问题与新对话交接（2026-10-17）

> 项目：`/Users/salmon/Desktop/InsightFlow`。负责人：三文鱼。**当前状态：PARTIAL / 未验收 / 未提交 / 未推送。** 只继续 Phase 8，完成后 STOP，等待三文鱼验收，不进入 Phase 9。本文件概括本轮问题与下一步；逐条原始证据见 [Phase 8 报告](32-Phase8架构验收与交付报告.md) 的“2026-10-17 最终版 v14 独立复验”。先核对当前 Git、代码、数据库与服务，不把本文件当作实时状态。

## 1. 本轮实际测到了什么

| 层级 | 最近独立结果 | 结论边界 |
| --- | --- | --- |
| Research 自动测试 | 267 passed；Ruff 全范围通过 | 证明已写入的契约与回归测试通过，不证明模型回答与来源在语义上完全吻合。 |
| 后端与数据库 | SQLite 956 passed / 11 skipped；全新隔离 PostgreSQL 16.2 967 passed；迁移 `upgrade/check → downgrade base → upgrade/check` 通过 | 只操作临时库，不对用户数据库降级。 |
| 前端 | lint 无警告、Vitest 144 passed / 15 files、生产构建 11 路由；v13→v14 未修改前端 | 不代替真实浏览器或内容质量。 |
| Tool 并发审批 | 先前隔离 PostgreSQL 专项 72 passed；真实浏览器 Prepare、Cancel、再 Prepare、Approve、重复批准与项目隔离通过 | 当前只有本地模拟 adapter；`SIM-...` 不是 Jira/Notion/飞书工单。 |
| 真实 Research | v14a/v14b 各 5 条，真实 Tavily + MiniMax-M3 + 全新隔离 PostgreSQL，合计 **2 completed / 8 partial_failed** | Codex 对照官方原文确认两条 OneDrive 的**窄事实**；整体可交付质量仍 FAIL。 |
| 真实浏览器 | 最终 Research 守卫后，FastAPI + Next.js 页面显示 OneDrive Run 前缀 `16a8c114` 为 Completed，5 Sources，Microsoft 官方引文；临时服务已关闭 | 证明接线、状态与显示，不证明所有 Research 问题都能回答。 |

Git HEAD 仍为 `449e879`。Phase 8 大量代码及本交接文档尚未提交；根目录两份用户原型 ZIP 与 `.playwright-cli/` 也未跟踪，不能盲目 `git add .`。Raw Feedback、`key/.env`、历史迁移与旧 Prompt 必须保留。**禁止 push。**

## 2. 问题、原因与当前处理

### A. 已修复：引文真实，但结论被扩写

Google Drive 真实 Run 曾把官方句子 “Find and tap the files you want to upload.” 扩写成“一次多选”或“只能逐文件点选”；另把 “Touch and hold the item.” 扩写成“避免多选的替代流程”。原因是旧校验只核对引用 ID/URL、产品名和 `Evidence` 引文是否出现在 snippet 中，没有约束引文外的断言。后续按词匹配来源的修复仍放过英文扩写；再下一版的停用词回退甚至可能让 `No support. Evidence: "..."` 通过。

当前 v14 守卫只允许三种完整描述：纯 `Evidence: "逐字引文"`、`From the cited source. Evidence: "逐字引文"`，以及全部引用满足官方 allowlist 时的 `From the official help page. Evidence: "逐字引文"`。引文外否定、尾随事实、第二引文和改写前缀会降级；明确询问多选/批量时还要求引文本身出现明确数量表述。对应回归已通过。**仍需审查标题、类别、平台和时间语境；字面引文不等于整个发现的语义获得证明。** 不得为提高完成率恢复词面匹配或放宽引用守卫。

### B. 未解决：真实 Research 产出率低，问题没有被完整回答

最终十条中只有两条 OneDrive `completed`。Microsoft Support 原文支持“双指滑动选择多个文件或照片”及“选择已有图片后点 Done”的窄事实；Google Photos、Dropbox、iCloud Photos、Google Drive 的目标问题在这两轮都没有可交付确认结论。实测可见的原因包括：检索结果与指定产品/平台/操作不够贴合、官方页面未直接写出所问的限制或数量、模型输出不能稳定满足严格的结构和引文格式。每条 `partial_failed` 的具体原因应重新查看 Run 和来源，不能把以上原因一概归给某一环。

更关键的是，当前 `completed` 门槛只要求至少一条带来源的 `competitor` 或 `market_pattern` 发现，**没有证明研究问题的全部关键条件都已回答**。v13b 的 Google Drive 普通上传步骤曾是来源支持的窄事实，却不能回答 v13a 的“一次多选”问题；单产品手势也可能被标为 `market_pattern`，分类含义仍需审查。新对话应先定义“可交付回答”的逐题判定：产品、平台、操作、数量、限制和时间语境是否均有原文支持；未获证据的部分应明确标为未验证。允许诚实的 `partial_failed`，不要为追求 10/10 `completed` 改宽校验。

最终十条 Run ID：

| 产品 | v14a | v14b |
| --- | --- | --- |
| Google Photos | `ca7f0301-fb62-4091-b06d-204aaa7038e2` · partial_failed | `5e3458d2-2b78-46c9-879a-360385c266c5` · partial_failed |
| Dropbox | `8f37be12-6f8f-46de-8f86-3dfa9359bcfc` · partial_failed | `3fa7ca34-de32-497a-8001-3815980b96d2` · partial_failed |
| OneDrive | `0bb2b5db-bda3-47c1-b1c8-b7eb3b90de3a` · completed | `8e15eb36-1fa3-4d5f-9390-5da23f1f2995` · completed |
| iCloud Photos | `b0d31a38-c67b-45bd-86dc-d9031ab82db8` · partial_failed | `f8d51c18-a421-4389-9545-72424c3b7b4d` · partial_failed |
| Google Drive | `2927e516-1722-49f7-8b05-2a2089c5a6f4` · partial_failed | `83a3291e-727e-402f-9dcb-38c0a4e94587` · partial_failed |

这些 UUID 属于已退出的临时数据库，不能当作可再次查询的持久地址。评测 JSON 当时位于 `/tmp/insightflow_phase8_human_eval_v14a.json` 与 `..._v14b.json`，可能被系统清理；持久脱敏摘要在 [正式报告](32-Phase8架构验收与交付报告.md)。

### C. 工程已通过、仍有边界的项目

- Tool 的并发 Prepare、双 Approve、Approve/Cancel 竞争、撤销批准、request ID 绑定、timeout/5xx 已用隔离 PostgreSQL 专项复验；最终浏览器有一条 cancelled 无结果和一条 completed 有 `SIM-...`，重复批准 ID 不变，跨项目访问 404。下一轮如果改动 Tool，须重新执行这些事务级回归。
- 真实 Jira、Notion、飞书写入与 MCP **DEFERRED**，不能称接入或验收通过；模拟审批只证明本地状态机。
- 当前浏览器使用隔离种子验证 Phase 8 页面和 Tool 流程；本轮没有从新 CSV 起点重走分类→聚类→Insight→评分→Requirement→PRD 的完整新建数据链路。若把它列作 Phase 8 验收范围，应单列 `NOT MEASURED`，不能由种子页面替代。
- 浏览器控制台曾有 `favicon.ico` 404、无评分时 Opportunity Score API 404；已观察到页面按空状态处理，属于低优先级独立问题，不是 Research 质量失败的根因。
- 历史 Phase 2 真语义 Embedding、Phase 3 人工 Groundedness、Phase 4 评分质量、Phase 6 未测指标、Phase 7 模型稳定性及人工验收限制继续有效，本阶段工程通过不能改写历史结论。

## 3. 下一对话的执行顺序与验收门槛

1. **先读并核对。** 完整阅读 `AGENTS.md`、[原 Phase 8 交接](31-Phase8新对话开发交接.md)、[正式报告](32-Phase8架构验收与交付报告.md)、[人工复测流程](33-Phase8人工复测流程.md)及项目规范、工作记录和原任务；再检查 `git status`、实际代码、数据库、API、运行进程和测试。旧文档中的 62/771/846 等数字是历史快照，不能称最新结果。保留所有未提交文件。
2. **先定位质量损失。** 对 v14 两轮十题按“查询→保留来源→模型原始结构→校验降级→最终发现”分层复核，区分检索缺页、官方来源约束、Prompt 格式失败和确实无公开证据。制定可复现的逐题评分表；禁止用模型自评代替原文核对。
3. **做最小修复。** 优先改善来源检索相关性和模型输出格式，使有明确官方原文的题能稳定得到窄而正确的回答；保持 v14 精确引文、官方 URL、项目隔离和完成门槛。对“部分事实有证据但问题未完全回答”应设计诚实状态或展示，不把局部引文包装成完整竞品结论。失败带具体 Run、引文和测试证据续接原 Claude Research `threadId`；同一工作树只允许一个写入者。
4. **独立复验。** 最终代码重新跑 Research 专项、后端 SQLite 全量、全新隔离 PostgreSQL 全量及 0012/0013 迁移往返、Ruff、前端 lint/test/build；再跑 5—10 条真实 Tavily/MiniMax Research，逐条人工对照官方来源并记录完整回答率、局部回答率、未验证率及误阳性。最终代码用真实 FastAPI + Next.js 浏览器检查 Research、Sources、项目切换和模拟审批全流程；不要连接用户现有数据库做 downgrade。
5. **报告与 Git。** 更新工作记录、验证记录和 Phase 8 报告，分开写自动测试、真实调用、Codex 人工来源判断、NOT MEASURED 和 DEFERRED。只有完整测试、Research 质量、审批和文档门槛都满足时，Codex 才可检查暂存内容并本地提交；排除密钥、原型 ZIP、Raw Feedback、临时数据与 `.playwright-cli/`。禁止 push。若质量仍失败，保持 `PARTIAL`、不提交，交付复测证据并 STOP。

## 4. 协作与安全信息

- Codex 负责范围、架构、独立验收和 Git。Claude 只改明确下发的文件，不运行 Git、不改 `.git`、`AGENTS.md`、`CLAUDE.md`、权限或历史迁移；Worker 期间 Codex 不写同一工作树。
- Research 原 Claude `threadId`：`8d048dd4-fb66-4838-898b-44f10cedd34c`。本轮续接 jobId：`ce0cb8d50af54cd4ade7c78afa880e0e`（600 秒超时）、`d5b2f620d1d2441ba6e1823c6ab48048`、`0b2965ad76734876be8509e9d4a1aa75`、`5adae484caa84001aa18dcfdbd7dee51`、`4f51273c19a5461aa0097678644ebb24`。更早 jobId 见正式报告。Tool 原 `threadId`：`520a4b77-2518-4ca2-9fc9-439b21245afb`；前端原 `threadId`：`42e973e0-5295-496d-a9bd-7c08d6d24ae4`。
- Claude Wrapper 使用 edit tier、本地 acceptEdits 映射、simple profile；每次保存 `jobId`。超时后先查进程、差异与独立测试，再用同一 `threadId` 带失败证据续修，不按 Worker 声明判断完成。
- `key/.env` 是用户本机配置。只在隔离进程内映射所需环境变量，不打印、修改或提交。保留两份原型 ZIP、Raw Feedback、旧迁移 0001—0011、旧 Prompt 与业务逻辑。

## 5. 可直接发给新对话的 Prompt

> 三文鱼让我继续担任 InsightFlow Phase 8 的 Codex Supervisor。项目路径 `/Users/salmon/Desktop/InsightFlow`。先完整阅读 `AGENTS.md`、`docs/34-Phase8测试问题与新对话交接.md`、`docs/31-Phase8新对话开发交接.md`、`docs/32-Phase8架构验收与交付报告.md`，再按交接读取规范、工作记录、原任务和人工复测流程。先核对当前 Git 差异、代码、数据库、API、进程与测试，不凭旧聊天或 Claude 完成声明推断状态，也不覆盖未提交文件。
>
> 当前 Phase 8 为 `PARTIAL`、未验收、未提交、未推送。最近最终代码的自动证据：Research 专项 267 passed；后端 SQLite 956 passed / 11 skipped；全新隔离 PostgreSQL 967 passed 与迁移往返通过；Ruff 通过；前端 144 passed、lint/build 通过。Tool 并发审批和浏览器模拟流程有通过证据，但真实 Jira、Notion、飞书与 MCP 未接入，`SIM-...` 只能称模拟。真实 Tavily/MiniMax 的 v14 十条为 2 completed、8 partial_failed；Codex 只确认两条 OneDrive 窄事实有 Microsoft 官方原文，Research 整体质量仍 FAIL。
>
> 请先分层定位八条未完成及局部回答的具体原因，特别检查检索相关性、官方来源、结构输出、引文校验和 `completed` 只代表一条带来源发现而非完整回答的语义缺口。保留 v14 对引文外扩写、无证据否定和多选数量词的严格守卫；不要为了提高完成率放松校验。指挥原 Claude Research 会话做明确文件范围内的最小修复，失败时给具体证据续接同一 `threadId`，保存每个 `jobId`。Codex 负责架构、独立测试、人工来源核对和 Git；单工作树同时只允许一个写入者。Claude 禁止 Git、权限、`AGENTS.md`、`CLAUDE.md`、密钥和历史迁移修改。
>
> 修复后重新执行最终代码的 SQLite、全新隔离 PostgreSQL 全量与迁移往返、Ruff、前端 lint/test/build、真实 FastAPI＋Next.js 浏览器审批与项目隔离；再跑 5—10 条真实 Research，逐条对照官方原文，区分完整回答、来源支持的局部事实、未验证和误阳性。保护两份用户原型 ZIP、Raw Feedback 与密钥，不对用户数据库降级。更新工作记录、验证记录和交付报告。只有完整测试、真实 Research 质量、审批与文档门槛全部满足时才由 Codex 本地提交；禁止 push。若质量仍失败，如实交付 `PARTIAL`、不提交。Phase 8 后立即 STOP，不进入 Phase 9。全程中文回答，并称呼我为三文鱼。

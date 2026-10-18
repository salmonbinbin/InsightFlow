# Phase 8 新对话开发交接（2026-10-16）

> 项目：`/Users/salmon/Desktop/InsightFlow`。负责人：三文鱼。当前结论：**PARTIAL / 未验收 / 未提交 / 未推送**。本文件是工作交接，不是 Phase 8 Delivery Report，也不代表历史 Phase 1—7 的语义或人工质量限制已经关闭。新对话只继续 Phase 8；完成后 STOP，等待三文鱼人工验收，不进入 Phase 9。

## 1. 先读与协作规则

先读根目录 `AGENTS.md`、`AGENT_HANDOFF.md`、`技术开发规范与Git规则.md`、`工作记录.md`、`README.md`，以及 `docs/11-长期开发总纲与Agent交接规范.md`、`docs/09-验证与交付记录.md`、`docs/08-决策记录与修正.md`、`docs/29-Phase7交付报告.md` 和三文鱼粘贴的 Phase 8 原任务。以当前代码、数据库、API、测试与工作记录为准；旧文档开头的阶段状态是历史记录。

Codex 管范围、架构、独立验收和本地 Git；Claude Code 只修改明确下发的文件。单工作树同时只允许一个写入者。Claude 不运行 Git、不改 `.git`、`AGENTS.md`、`CLAUDE.md` 或权限；失败带证据续接原 Claude `threadId`。本阶段满足全部提交门槛后才由 Codex 本地提交，**禁止 push**。两份根目录的 InsightFlow 原型 ZIP 是用户已有文件，保留且不要纳入提交；密钥文件、缓存、临时数据库也不能提交。

## 2. 当前 Git 与文件状态

- HEAD：`449e879 style: align Phase 7 frontend with revised prototype`。Phase 8 的 Research、Tool、前端及本交接文档都还没有提交。
- 当前工作树有大量已修改及未跟踪文件。已修改包括 `backend/app/db/config.py`、`backend/app/main.py`、`backend/app/models/__init__.py`、前端 Insight/Requirement 详情、typed API、Mock、样式及测试。未跟踪包括迁移 `0012_phase8_research.py`、`0013_phase8_tools.py`，Research/Tool 模型、API、Schema、服务、Prompt、测试，以及两份原型 ZIP。
- 新对话开始先运行 `git status`、`git log --oneline -10` 和差异检查；不要清理或覆盖当前工作树。历史迁移 0001—0011、Raw Feedback、既有 Prompt/业务逻辑必须保护。

## 3. 已实现但仍需最终验收的功能

### Research Agent

已加 `research_runs`、`research_sources`、`research_findings` 与 0012 迁移；Tavily 搜索、MiniMax-M3 结构输出、版本化 `research-agent-v1/v2` Prompt、来源 ID/URL 校验、有限重试和调用计量。`POST /insights/{insight_id}/research`、Run/历史/Sources/Findings 查询 API 已接线。Insight 详情把 Internal Evidence 与 External Research 分区显示，支持运行、状态、发现及来源。只有至少一条有外部来源的竞品或市场发现时 Run 才可 `completed`；否则为 `partial_failed` 或 `failed`，不能把无来源推断显示成已证实结论。

Research 工程测试曾单独 **86 passed**；前端 Research 历史漏传必需 `project_id` 已修复。真实质量仍未过门槛，见第 5 节。

### Tool / Human Approval

已加静态工具注册表、0013 迁移、`tool_runs` 审计和 `simulated_issues` 模拟目标表。只实现 `simulated_issue_tracker.create_issue` 的准备、冻结预览、取消、人工批准、`SIM-...` 模拟结果、请求 ID 幂等及有限重试；**真实 Jira、Notion、飞书写入与 MCP 均未接入**。Research 的 `web_research.search` 在注册表中为只读能力描述，并非通用 Agent 编排器。Requirement 详情已接 Tool Action Preview，明确显示“仅模拟”，不会伪造真实外链。`SIM-...` 不是外部工单。

最新修复：准备后若 Requirement 已不再 `approved`，批准接口在调用模拟 adapter 前返回 409，保持冻结预览且不写 `simulated_issues`；名称等内容变化但仍获批准时，批准使用原冻结快照。修复来自原工具 Claude 会话，已独立复验专项 **62 passed**。**并发批准/取消和并发 Prepare 的事务行为尚未完成可靠验证或修复**，尤其要防止双 adapter 调用、裸 500 和跨 Requirement 重用 request ID。

### 前端 Tool Action Preview

已完成 Requirement 详情的 Prepare、Preview、Cancel、Approve、失败重试、项目切换状态清理和重复点击守门。原前端 Claude 会话首轮造成 5 个测试失败，续接同会话后已修复：取消后仍显示取消结果、Mock 按路径注入错误、测试 URL 断言包含 `project_id`。Codex 最新独立复验：lint 无警告；**144 passed / 15 files**；生产构建 **11 路由通过**。这仍不等于真实浏览器 E2E 已通过。

## 4. 本次独立测试的准确状态

| 项目 | 本次可确认结果 | 边界 |
| --- | --- | --- |
| Frontend lint / Vitest / build | PASS；lint 无警告，144 passed，11 路由构建通过 | 尚未对 Phase 8 页面做真实浏览器 + 后端 + PostgreSQL 联调 |
| Tool 专项 | PASS；62 passed | 并发事务边界未测完 |
| Backend SQLite 全量 | PASS；**771 passed, 5 skipped** | PostgreSQL 专项在这次最终代码上尚未重跑 |
| Ruff | PASS；`app tests alembic scripts` 全范围 | 不代表真实模型质量 |
| 0013 迁移往返 | **NOT MEASURED（最终代码）** | 需隔离 PostgreSQL `upgrade/check → downgrade base → upgrade/check` |
| 真实 Research 质量 | **FAIL** | 5 次真实 v2 抽样没有可交付的来源支持竞品结论 |
| 真实 Tool 外部写入 | **DEFERRED** | 目前只有数据库内模拟执行，不得称为 Jira/Notion/飞书集成 |

早先工具层加入前的 `702 passed, 5 skipped` / PostgreSQL `707 passed` 以及工具专项首轮 `61 passed` 只是历史快照，不能替代上述最新结果或最终 PostgreSQL 验收。

## 5. 真实 Research 抽样与质量阻断

隔离 PostgreSQL + 真实 Tavily + 真实 MiniMax-M3 的 5 条 v2 抽样记录在 `/tmp/insightflow_phase8_real_research_eval_v2.json`（临时路径，可能被系统清理）；单次模型输出抓取在 `/tmp/insightflow_phase8_capture_payloads.json`。新对话应先复制/整理**脱敏后的**样本摘要到持久项目报告，再按最新代码重新实测。不要复制任何密钥或原始用户反馈。

| 查询对象 | 实际状态 | 主要问题 |
| --- | --- | --- |
| Google Photos | `partial_failed` | `source_refs_invalid`；来源中混有社区、非官方和不对应移动端问题的资料 |
| Dropbox | 抽样时曾 `completed` | 只有未验证的内部 Insight、Opportunity、Risk 推断，没有带来源的竞品发现；完成门槛修复后同类输出应为 `partial_failed` |
| OneDrive | `partial_failed` | `unknown_top_level_field` |
| iCloud Photos | `partial_failed` | `unknown_top_level_field` |
| Google Drive | `partial_failed` | `description_invalid`；部分来源偏离查询对象 |

这 5 条还没有满足“竞品信息真实且来源能支持发现”的人工抽查要求。结构通过、URL 可访问、引用 ID 合法与语义支持度是不同检查，不能互相冒充。建议先定位 v2 输出与 Schema 不匹配的原因，控制查询与来源相关性，再重新运行 5—10 条真实 Insight Research，逐条记录来源支持与事实/推断边界。质量未过时保持 Phase 8 `PARTIAL`，不能提交。

## 6. Claude 会话与 jobId

- Research 后端原会话 ID：**需从本机 Claude job 历史定位**；不要猜测。失败修复应尽量续接原会话。
- Tool 后端原 `threadId`：`520a4b77-2518-4ca2-9fc9-439b21245afb`。工具层并发大任务 `5a22cc05461f4640a692bd65a6a72f58` 超过 600 秒停止，未产生这轮代码修复；缩小范围后的审批资格修复 job `9c8dbec4cf514bba9787948d69d6b0ab` 已完成并通过专项测试。
- 前端原 `threadId`：`42e973e0-5295-496d-a9bd-7c08d6d24ae4`。Tool Preview 初始 job `140310ce01634f3f8736ccbc98fc7f20`，修复 5 个测试失败的续接 job `58f4deed295047e2a82e1c1d10b88536`；Codex 已独立跑绿 144 项。

Claude Wrapper 使用 `tier=edit`、本地映射的 `acceptEdits`、`runtimeProfile=simple`。Worker 自述或 job 完成不等于验收；每轮结束看实际差异并由 Codex 重测。不要让 Claude 运行 Git、触碰权限设置或扩张任务文件范围。

## 7. 新对话按顺序完成

1. 核对文件与当前进程、服务端口和隔离数据库；保留用户 ZIP、Raw Feedback 和未提交 Phase 8 代码。
2. 对 Tool 并发 Prepare / Approve / Cancel 做 PostgreSQL 事务级测试与最小修复；撤销批准后不执行、重复批准只有一条模拟目标、取消后零写入、provider timeout/5xx、错误响应均需验证。特别审查 `request_id` 与 Requirement 的绑定、行锁/事务边界。必要修复给原 Tool Claude `threadId`，每个 jobId 保存。
3. 改善 Research 真实输出结构与查询相关性；保留严格 Schema/grounding 守卫。至少 5—10 条真实 Research 逐条核对来源是否支持竞品能力、限制、机会和风险，记录成功率/失败率，不以 Mock 或仅 URL 合法代替。
4. 在**全新隔离 PostgreSQL** 跑后端全量及 0012/0013 迁移 `upgrade/check → downgrade base → upgrade/check`；前端 lint、测试、build 在最终改动后重跑。运行真实 FastAPI + Next.js 浏览器，检验 Insight Research、Sources、项目切换、批准的 Requirement Tool Preview、Cancel、Approve、重复操作和 `SIM-...` 标记。真实浏览器本轮尚未执行。
5. 更新 `工作记录.md`、`docs/09-验证与交付记录.md`、必要时 `docs/08-决策记录与修正.md`，并新增正式 Phase 8 架构/交付报告与人工复测步骤。报告单列设计、实现、自动测试、真实调用、人工核对、未测和 deferred 的事项；历史 Phase 2/3/4/6/7 质量限制继续保留。
6. **只有**完整测试、真实 Research 质量、审批流程及文档四门槛均通过，才由 Codex 检查暂存内容、排除密钥/ZIP/临时数据并本地提交；不 push。否则以 `PARTIAL` 交接未提交状态和复测证据。Phase 8 后 STOP。

## 8. 人工复测最短路径（最终版报告仍需补齐具体 ID）

使用隔离测试数据启动 backend 和 frontend → 打开一个 Insight → 运行 Research → 观察状态并展开竞品发现与来源 → 对照来源原文核实是否真的支持发现 → 确认 Internal Evidence 与 External Research 分离 → 完成 PM Decision、Requirement、PRD → 在已批准 Requirement 准备模拟操作 → 核对冻结 Title/Description/Priority/Source Insight → Cancel 并确认无模拟目标 → 再次 Prepare → Approve → 查看 `SIM-...` 和审计记录 → 重复 Approve 确认没有第二条目标。**此流程目前是待执行的复测说明，不是已跑通的结果。**

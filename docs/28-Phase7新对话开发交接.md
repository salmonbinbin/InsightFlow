# InsightFlow Phase 7 新对话开发交接

> 2026-10-15。先读 [当前进度审计报告](27-Phase7当前进度审计报告.md)和用户原始 Phase 7 任务全文（原始粘贴文件：`/Users/salmon/.codex/attachments/370043d7-56bc-4760-bae6-d7a3af603612/已粘贴的文本.txt`）。本文件是接手索引，不代替即时检查。

## 当前状态和 Claude 会话

- 项目：/Users/salmon/Desktop/InsightFlow。HEAD=33105c1，Phase 7 **未提交**。先执行 git status --short 和 git log --oneline -10，保护现有工作树，不重置或清理。
- Claude 原会话 threadId=03c20a03-1a17-4b6e-af35-03d69b8cafa0。尽量用 claude_reply_start 续接同会话，tier=edit、permissionMode=auto（本地 wrapper 映射 acceptEdits）、runtimeProfile=simple；每个 jobId 都记录。
- 旧 CSV 修复 jobId=e6df93ef9349477ca99c91cb4d9d1620 的 MCP 登记仍显示 running，但本轮 ps -p 69352,69353 无进程。**这是登记与进程状态不一致，既非已完成证据，也不意味着仍有写入者**。新对话先复核进程、文件和 job 状态，再继续。当前 CSV 缺陷仍在源码中。
- 单一工作树同时只有一个写入者。Claude 写代码时 Codex 不改文件；Claude 完成后 Codex 独立审查、补 QA 测试和验收。Claude 禁止所有 Git 命令、commit/push、修改 .git、AGENTS.md、CLAUDE.md、权限设置或脚本绕过；只运行明确下发的测试。

## 必读与基线

按原任务读取工作记录.md、README.md、docs/11-长期开发总纲与Agent交接规范.md、docs/09-验证与交付记录.md、docs/08-决策记录与修正.md、docs/02-V1-PRD.md、docs/05-数据库与API设计.md，以及新版原型 ZIP、docs/26-Phase7新版原型审核修订复验报告.md、Phase 6 报告和当前 OpenAPI/routes/schemas。还应读 AGENT_HANDOFF.md 与 技术开发规范与Git规则.md。

2026-10-15 Codex 独立验证：前端 100/100、lint/build 通过；后端默认 SQLite 615 passed/5 skipped，Ruff 通过。**修改后必须重新验证**。PostgreSQL、真实浏览器和 AI E2E 本轮未执行。

## 建议继续顺序

1. 修 PRD CSV 连续输入：先只下发 frontend/src/app/prds/[id]/page.tsx 最小范围给 Claude；Codex 加连续键入两个 ID 的交互测试，复跑前端门禁。
2. 补 Analysis 项目级持久 Run 历史与隔离：后端 AnalysisRun 承载分类/聚类，InsightRun 承载洞察。只补必要项目级只读列表、typed client 与页面；按 ID 读取/重试需校验 project_id。增加项目隔离、分页、排序、空项目测试并更新 docs/05。
3. 补 Feedback Detail AI Analysis：最小项目隔离只读接口，明确“最新/指定 Run”的确定性选择规则，展示真实全字段；覆盖有/无分析、跨项目、切换项目、失败重试。Raw Feedback 不可改。
4. 修 Evaluation 100 条以上的分页或明确截断，并覆盖实际值/目标值、null/0、项目切换和跨项目状态；评估 Next.js 同系列修补升级；真实浏览器检查各目标宽度、键盘与焦点。
5. 做完整回归：前端 lint/test/build；后端 pytest/Ruff；项目既有隔离 PostgreSQL 验证。真实启动 FastAPI、PostgreSQL、Frontend，完成 Create Project → Upload CSV → Classification → Clustering → Insight → Evidence → Opportunity Score → PM Decision → Requirement → PRD → Evaluation，并验证 Defer 禁止创建 Requirement、项目隔离和旧 PRD 版本可查看。记录 HTTP 和持久化证据。
6. 更新工作记录.md、docs/09-验证与交付记录.md；重要原型/API 调整才更新 docs/08。提供交付报告及每一步有预期结果的人工复测流程。四项硬门槛全部通过后 Codex 才可本地 commit；禁止 push；交付后 STOP。

## 凭证和提交排除

key/.env 是用户本地 JSON 凭证配置，不是 dotenv。只读非秘密元数据，在隔离进程中映射必要环境变量，不打印、不修改、不提交。根目录两份原型 ZIP、.env*、node_modules、.next、临时数据库、缓存、frontend/tsconfig.tsbuildinfo 均不要提交。提交前检查 git diff、git status 及 staged 路径。

## 可复制到新对话的提示词

```text
三文鱼授权你继续 InsightFlow Phase 7 Frontend MVP 的 Codex Supervisor → Claude Code Worker 开发，项目在 /Users/salmon/Desktop/InsightFlow。请全程用中文称呼我“三文鱼”。

先完整阅读 docs/27-Phase7当前进度审计报告.md、docs/28-Phase7新对话开发交接.md、原任务文件 /Users/salmon/.codex/attachments/370043d7-56bc-4760-bae6-d7a3af603612/已粘贴的文本.txt、AGENTS.md、AGENT_HANDOFF.md、技术开发规范与Git规则.md、工作记录.md、README.md、docs/05-数据库与API设计.md、docs/09-验证与交付记录.md、docs/26-Phase7新版原型审核修订复验报告.md；核对当前代码、Git 状态、Claude 任务登记和进程。保护未提交 Phase 7 工作树，不重置。

Codex 负责范围、架构、独立审核/测试和 Git；Claude 只实现明确下发的文件。单工作树同一时间只有一个写入者。尽量续接 Claude threadId=03c20a03-1a17-4b6e-af35-03d69b8cafa0，使用 MCP edit tier、auto/本地 acceptEdits、simple；保存每个 jobId。Claude 不得运行任何 Git 命令、commit/push、修改 AGENTS.md/CLAUDE.md 或权限配置，且只运行明确授权的测试。缺陷由 Codex 给复现证据后让原会话返工。

按交接顺序先修 PRD CSV 连续输入，再补 Analysis 项目级持久 Run 历史与隔离、Feedback Detail 真实 AI Analysis、Evaluation 分页/项目隔离，随后审查升级 Next.js 与响应式/可访问性。任何最小后端 Read API 均需项目隔离测试及文档。每包完成后 Codex 独立验收。不得将 Mock、SQLite、单次模型调用或未审核数据集冒充真实 E2E/语义质量。

最终重跑前端 lint/test/build、后端 pytest/Ruff、隔离 PostgreSQL，完成真实 Frontend→FastAPI→PostgreSQL→AI Pipeline 主流程及异常流程，并更新工作记录.md、docs/09（必要时 docs/08）。只有 Frontend Tests PASS、Backend Regression PASS、Real E2E PASS、Docs Updated 四项都成立，Codex 才能本地 commit；绝不 push。输出 Phase 7 Delivery Report 和逐步注明正常预期的人工复测流程，交付后 STOP，不进入 Phase 8。保留 Phase 2/3/4/6 历史限制，未测项写 NOT MEASURED。
```

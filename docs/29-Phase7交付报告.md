# InsightFlow Phase 7 Delivery Report

> 2026-10-15；负责人：三文鱼；执行：Codex Supervisor → Claude Code Worker。范围仅 Phase 7 Frontend MVP。**Status：COMPLETED（工程交付）；人工产品与语义验收待三文鱼。STOP，不进入 Phase 8。**

## Prototype Implemented / Frontend Stack

正式实现采用 Next.js 14.2.35、React 18.3.1、TypeScript、Vitest。新版 OpenDesign 原型的页面结构与关键流程已落地，数据来自真实 FastAPI。Next.js 将浏览器同源 `/api/*` 转发至后端；代理超时为 400000ms，满足同步模型调用及有界重试。浏览器不会从静态 Mock JSON 读取业务结果。

## Routes / Pages / Components

11 个构建路由：`/`、`/upload`、`/feedback`、`/analysis`、`/insights`、`/insights/[id]`、`/requirements`、`/requirements/[id]`、`/prds`、`/prds/[id]`、`/evaluation`。AppShell、项目切换器、Feedback Detail、RunCard、加载/空/错误/重试组件与 typed API client 已接线。移动端表格局部滚动；1440、1280、1024、820、390px 无整页横向溢出，导航可键盘聚焦。

## Backend APIs Integrated / Minimal Backend Changes

前端接入 Project、CSV Import、Feedback、Analysis/Clustering/Insight Runs、Insight Evidence、Opportunity Score/Decision、Requirement、PRD、Evaluation 既有 API。Phase 7 最小补充：项目级 Analysis/Insight Run 历史、PRD/Evaluation 列表、单条 Feedback AI Analysis、Evidence 原文和来源字段、Evaluation 项目隔离查询及前端分页所需响应。契约见 [数据库与 API 设计](05-数据库与API设计.md)。PRD 客户端对工具协议/传输错误有界重试；13 节内容、Evidence、六类边界场景、商业承诺和可验证验收标准仍在入库前严格检查。历史 Prompt v1/v2 保留，新生成记录使用 `prd-generation-v3`。

## Core User Flows / Real E2E Result

**PASS。** Codex 在临时 PostgreSQL 16.2、真实 MiniMax-M3、真实 FastAPI 与浏览器同源 Next.js `/api` 代理上独立完成：Create Project → Upload CSV（12 条）→ Classification（12/12）→ Clustering → Insight（最终 Run：1 draft、9 needs_validation）→ F001/F002/F003 Evidence 原文 → Opportunity Score → Business Value 7.5 → PM Accept/P2 → Requirement 创建与编辑 → AI PRD v1（201，读回，Prompt v3）→ PM 编辑保存 → AI 再生成 v2（201）→ 旧版查询与版本列表（2、1）→ v2 Accept → Evaluation（7 metrics、12 cases）。真实浏览器读回 Feedback、Analysis、Insights、Requirement、PRD 与 Evaluation；Evaluation 运行行可实际点击，展示 Actual/Target 和“未经人工复核”。

异常流程：Defer 后创建 Requirement 返回 422；项目 B 六类列表总数为 0，以 B 查 A 的反馈分析及 Evaluation 详情返回 404。PRD 早期真实失败依次包含缺失 `tool_use`、`verifiable=false`、错误风险项类型、上游传输失败和未经来源支持的商业承诺；这些请求均未创建不合规版本。Prompt 版本修正后两次真实 AI PRD 生成与持久化成功。此结果证明工程链路可用，不证明上游模型每次稳定或 PRD 语义质量。

生产构建的真实浏览器也点击了版本 1 的“再生成新版本”：前两次模型内容校验失败，UI 正确显示重试且数据库仍只有 2 个版本；第三次返回 201，页面跳转至新 PRD，版本 3 可读回。该项证明按钮到 Next.js 代理、FastAPI、PostgreSQL、模型的闭环；连续失败保留为稳定性限制。

## Tests Added / Results

| Gate | 独立结果 |
| --- | --- |
| Frontend Tests | **PASS**：lint 无警告/错误；14 文件 **106 passed**；Next.js 生产构建 11 路由通过 |
| Backend Regression | **PASS**：SQLite **623 passed, 5 skipped**；Ruff 通过；隔离 PostgreSQL **628 passed**；迁移 upgrade/check → downgrade base → upgrade/check 与 `POSTGRES_VERIFICATION_PASSED` |
| Real E2E | **PASS**：上述真实 Frontend → FastAPI → PostgreSQL → AI 与异常流程；真实浏览器五种宽度复测 |
| Docs Updated | **PASS**：工作记录、验证与交付记录、API 设计、决策记录、前端 README、本报告和人工复测流程已更新 |

新增测试覆盖前端 API/页面交互、CSV、项目切换、PRD 连续输入、Evaluation 分页；后端项目级列表、Run 历史、Feedback AI Analysis、Evidence 来源和 PRD 有界重试。旧测试和 0001—0011 迁移未删除或回写。

## Prototype vs Implementation Differences

原型中的 Mock 数字、sessionStorage 编辑与本地 CSV 演示改为后端 API 和数据库持久化；Analysis 历史从项目级 Read API 加载；Evaluation 列表只读，创建 EvaluationRun 仍由既有 API 负责。正式 AppShell 类名与原型 CSS 不同，已补齐生产侧栏、导航、顶栏和键盘焦点样式。无身份系统；项目隔离是项目范围查询与前端状态清理，不构成多用户鉴权。

## Known Issues / UX Debt / Technical Debt

- 真实模型曾多次给出协议、结构和 grounding 失败；当前成功样本不能推导稳定成功率。严格校验保持，失败可重试并需查看 503 原因。
- Evaluation 示例参考标签是 `synthetic_reference_labels_unreviewed_no_human_review`；本轮 Run 状态 `partial_failed`，界面如实展示。指标缺失显示 `NOT MEASURED`，0 不被当作缺失。正式标签与 Groundedness/Hallucination 人工复核尚未完成。
- Phase 2 真实语义 Embedding 未验收，Phase 3 人工语义复核未关闭，Phase 4 评分/理由业务质量、Phase 6 部分指标仍按历史报告维持 `NOT MEASURED` 或 `PARTIAL`。本阶段通过不修改历史结论。
- 分析与生成仍同步执行，长耗时依赖 Next.js 代理超时；Evaluation 看板无创建按钮；概览部分聚合指标仍为 N/A。后续是否优化由三文鱼在本阶段人工验收后决定。

## Files Changed / Git Commit

前端 `frontend/src/`、配置和锁文件；后端 API/schema、PRD Prompt v2/v3、客户端、服务与 Phase 7 测试；项目文档。两份原型 ZIP、`key/.env`、`.next`、`node_modules`、`tsconfig.tsbuildinfo`、临时数据库不属于提交。最终本地提交由 Codex 在四门槛均 PASS 后执行，**不 push**；提交哈希以 Git 实际结果为准。

## Local Run Steps

在 `backend` 先准备本机模型环境变量 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`（勿粘贴密钥到命令或报告），安装依赖后执行 `uv run --with pgserver==0.1.4 python scripts/dev.py`；它使用 `work/local-postgres` 的独立 PostgreSQL 并迁移至 head，后端监听 `127.0.0.1:8000`。另一终端在 `frontend` 执行 `npm ci`，再执行 `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000 npm run dev`，打开 `http://127.0.0.1:3000`。不要把真实模型凭证或生产数据库带入人工演示。

## User Acceptance Steps / Next Recommended Phase

逐步操作及每步预期见 [Phase 7 User Acceptance Test](30-Phase7人工复测流程.md)。三文鱼完成产品、语义和界面人工复核后再判断 Phase 7 最终 Gate。下一阶段仅在三文鱼明确批准时讨论；本交付到此 **STOP**。

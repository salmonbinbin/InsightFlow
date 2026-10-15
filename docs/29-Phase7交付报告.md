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
| Frontend Tests | **PASS**：lint 无警告/错误；15 文件 **107 passed**；Next.js 生产构建 11 路由通过 |
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

## 2026-10-15 补充联调与修复验收

三文鱼要求打开正式页面继续测试。Codex 在同一 Claude 会话 `03c20a03-1a17-4b6e-af35-03d69b8cafa0` 下发三包前端修复：项目弹窗可见可点击（`2667f3afc3254c9a84e97820a73be457`）、概览读取真实分类 Run 和洞察分页 `total`（`f7cf9a406bf449748b9dbe0442df8ac0`）、项目切换时按项目 ID 阻断旧快照（`be1048507bf24ef79124f11ea392ceb9`）。首次生产构建发现数组首项可能为 `undefined` 的严格类型错误，Codex 提供构建行号并续接原会话修复（`ea258b61b92544a49d425d378101c275`）。Codex 独立增加概览回归测试并修正测试客户端的项目洞察过滤。

最终前端 lint **PASS**、Vitest **107 passed / 15 files**、按 `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:18000` 构建 **11 路由 PASS**。后端本轮已独立复验 SQLite **623 passed, 5 skipped**、Ruff **PASS**、隔离 PostgreSQL **628 passed** 与迁移往返 **PASS**；本补充修复没有改后端。

生产浏览器重新实测 `http://127.0.0.1:13000`：项目 A 概览反馈 **12**、最新分类 **成功 12 / 输入 12**、洞察 **1**；切换空项目 B 后反馈和洞察均 **0**、分类为“尚未运行”，A 的原文和洞察不显示；切回 A 数据恢复。390px 弹窗可打开和选择项目，文档及 body 宽度均为 390px。控制台仅见 `favicon.ico` 404，未影响功能。测试服务使用隔离 `work/phase7-user-postgres`、后端 `127.0.0.1:18000`、前端 `127.0.0.1:13000`；与其他项目占用的 8000 端口隔离。

本次模型链路的真实边界：开启聚类模型复核时，Run `e744d448-ce9f-4e25-9661-18ac32f32657` 两次因 `split_partition` 校验失败成为 `partial_failed`，随后洞察生成被 409 正确拦截。将**本隔离演示服务**设为 `CLUSTER_REVIEW_ENABLED=false` 后，算法聚类 Run `ea7e9061-7f4d-4763-8e01-fa91ec5df4fc` 完成，后续洞察、Requirement、PRD 仍走真实模型、FastAPI 和 PostgreSQL。Requirement 与 PRD 首次各遇 503、再次成功；失败请求没有创建记录。故本次证明可配置主链路端到端可用，**聚类模型复核未通过本轮验收**，不能以关闭复核后的成功替代它。真实语义质量与三文鱼的人工产品验收仍待复核。

## Files Changed / Git Commit

前端 `frontend/src/`、配置和锁文件；后端 API/schema、PRD Prompt v2/v3、客户端、服务与 Phase 7 测试；项目文档。两份原型 ZIP、`key/.env`、`.next`、`node_modules`、`tsconfig.tsbuildinfo`、临时数据库不属于提交。最终本地提交由 Codex 在四门槛均 PASS 后执行，**不 push**；提交哈希以 Git 实际结果为准。

## Local Run Steps

本次隔离联调服务已启动时，直接打开 `http://127.0.0.1:13000`；FastAPI 健康检查为 `http://127.0.0.1:18000/health/ready`。演示项目 A 与空项目 B 已保留在隔离库中。该服务的聚类模型复核设置为关闭，仅用于复测本轮主链路与页面；需要检验复核本身时另起隔离运行并开启该配置。

在 `backend` 先准备本机模型环境变量 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`（勿粘贴密钥到命令或报告），安装依赖后执行 `uv run --with pgserver==0.1.4 python scripts/dev.py`；它使用 `work/local-postgres` 的独立 PostgreSQL 并迁移至 head，后端监听 `127.0.0.1:8000`。另一终端在 `frontend` 执行 `npm ci`，再执行 `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000 npm run dev`，打开 `http://127.0.0.1:3000`。不要把真实模型凭证或生产数据库带入人工演示。

## User Acceptance Steps / Next Recommended Phase

逐步操作及每步预期见 [Phase 7 User Acceptance Test](30-Phase7人工复测流程.md)。三文鱼完成产品、语义和界面人工复核后再判断 Phase 7 最终 Gate。下一阶段仅在三文鱼明确批准时讨论；本交付到此 **STOP**。

## 2026-10-15 修订版原型视觉返工与复验

三文鱼指出正式页面的按钮、窗口样式覆盖不足。Codex 将修订版原型与正式页面逐页对照，确认原因是原型 CSS 与正式 React 类名不一致。Claude Code 在原会话 `03c20a03-1a17-4b6e-af35-03d69b8cafa0` 中只修改 `frontend/src/styles/app.css`；Codex 独立检查差异、构建和浏览器行为。视觉返工 job：`03780e60e7364ca0b9414cfb1dbc488c`、`3dedbb7a2e394679962be311349a98c4`、`ac7007012dbf4409a5c6f9083fdb3076`；浏览器缺陷续修 job：`7656f0a32878432fae6e2c2e8da04fe5`、`3c2ce9b3703e44698f22d577d5d8ed59`、`3dc315464cc44a2597f3c17aa68c4ae6`、`23673f587c9d4074a89393918a844c49`、`a53204490e1c41ca822059f68ad3dfc6`。

本次补齐了导航、页头、指标卡、表单、数据表、详情区、状态提示、操作按钮、项目弹窗、反馈抽屉、PRD 和评测区域的正式类名样式。真实浏览器一度发现反馈抽屉只显示遮罩、项目弹窗右侧超出手机屏幕 46px、洞察详情评分表及底部操作区撑宽页面；均根据 DOM 边界测量续修。最终 390px 下项目弹窗为 left 16/right 374、反馈抽屉为 left 19.5/right 390 且关闭按钮在屏幕内；评分表自身 right 341、clientWidth 290、scrollWidth 777，可在表内滚到末列；洞察 CTA 的两个按钮 right 341，页面 main/body scrollWidth 均为 390。

Codex 最终独立运行前端 lint **PASS**、Vitest **107 passed / 15 files**、带 18000 后端地址的生产构建 **11 路由 PASS**。隔离 PostgreSQL、FastAPI 与生产 Next.js 重启后，11 个路由在 390px 和 1440px 均返回 HTTP 200、正常显示标题；手机全部 main/body scrollWidth=390，桌面 body=1440。项目弹窗打开/关闭、反馈详情抽屉打开/Escape 关闭、PRD 长 Evidence 文本换行、洞察评分表局部滚动及 CTA 按钮边界已用真实浏览器验证。本轮只改 CSS；前轮后端回归与真实 AI 主链路证据沿用上文，聚类模型复核失败和人工语义限制不变。

**与原型仍有结构差异：** 原型首页的 528 等汇总、趋势、导出和设置为静态 Mock/演示交互，正式首页显示数据库中的 12 条反馈、最近分类 12/12、洞察 1，缺失指标明确标 N/A；本次视觉返工未引入虚构统计或本地伪持久化。页面视觉向修订版原型靠拢，但不宣称像素一致或原型所有 Mock 控件都已成为正式功能。

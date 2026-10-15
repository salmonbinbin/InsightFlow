# InsightFlow Phase 7 当前进度审计报告

> 审计日期：2026-10-15。对象：当前本地工作树。结论：**PARTIAL / 未达到提交门槛**。

## 授权与当前 Git 状态

三文鱼授权 Phase 7 Frontend MVP，由 Codex 监督 Claude Code 实现并独立验收。只有前端测试、后端回归、真实 Frontend → FastAPI → PostgreSQL → AI Pipeline 端到端和文档门槛全部通过后，才能本地提交；禁止 push，交付后 STOP。旧 AGENTS.md 的 Phase 4 范围是历史文本，本次 Phase 7 授权优先；单写入者、Claude 禁用 Git、保护历史数据等限制继续适用。

当前 HEAD 为 33105c1（Phase 6 基线），Phase 7 改动大量留在工作树，尚无本阶段提交。**不能宣布 Phase 7 完成，也不能提交。**

## 已落地范围

| 范围 | 当前情况 |
| --- | --- |
| 骨架 | Next.js 14 / React 18 / TypeScript、项目选择器、typed API client、状态组件已存在 |
| 页面 | Overview、Feedback、Upload、Analysis、Insights/Detail、Requirements/Detail、PRD List/Detail、Evaluation 共 11 个路由可构建 |
| 核心链路 | Insight/Evidence 原文及来源、Opportunity Score、AI/PM Priority、PM 决策、Requirement 编辑、PRD 13 节编辑及版本动作已有代码和专项测试 |
| Evaluation | 项目历史、Run、Actual/Target、metrics、cases 接 API；null 显示 NOT MEASURED，0 保留 0 |
| 最小后端补充 | Evidence 来源及对应分类分析字段、项目级 PRD/Evaluation 列表；SQLite 专项测试存在 |

原型报告 docs/26-Phase7新版原型审核修订复验报告.md 结论是**静态交互原型 CONDITIONAL PASS**；其中 Mock 汇总和演示数字不是正式数据验收证据。

## 2026-10-15 本轮独立实测

| 命令 | 结果 |
| --- | --- |
| cd frontend && npm test -- --run | 12 文件、**100 测试通过** |
| cd frontend && npm run lint | **通过**，无 ESLint 警告/错误 |
| cd frontend && npm run build | **通过**，Next.js 14.2.18，11 个路由完成构建 |
| cd backend && uv run pytest -q | **615 passed, 5 skipped**；默认环境，不等于 PostgreSQL |
| cd backend && uv run ruff check app tests alembic scripts | **通过** |

本轮未运行隔离 PostgreSQL、真实浏览器主流程、真实模型及完整前后端持久化 E2E；这些验收项均为 **NOT MEASURED**。

## 未完成问题

1. **P1 PRD CSV 输入**：frontend/src/app/prds/[id]/page.tsx 的 EvidenceCsvField / SourcesCsvField 每次键入都 csvParse 后 csvJoin，尾随逗号被立即抹掉，影响连续输入第二个 ID。需修复并加真实连续键入测试。
2. **P0 Analysis 历史和项目隔离**：frontend/src/app/analysis/page.tsx 的历史仅在 sessionStorage，按 ID 加载分类 Run 后未核对响应 project_id；聚类及 Insight 入口也要审查。需最小项目级只读 Run 列表、前端真实历史与跨项目拒绝测试。
3. **P1 Feedback Detail AI Analysis**：frontend/src/app/feedback/page.tsx 打开抽屉只执行 setClassification(null)，未读取分类分析，AI 区块实际为空。需项目隔离只读接口、完整 AI 字段展示及测试，保护 Raw Feedback 原文。
4. **P1 Evaluation 截断**：metrics/cases 只取前 100 条，无后续分页或明确截断提示。还需测试项目切换、错误重试、跨项目返回及 null/0。
5. **P1 依赖/UI**：Next.js 锁定 14.2.18；评估同系列修补升级。响应式 1440/1280/1024/820/390、键盘和焦点尚无本轮真实浏览器验收。
6. **提交硬门槛**：真实 Frontend → FastAPI → PostgreSQL → AI Pipeline 全链路未验证。Mock、SQLite 和单次模型 HTTP 成功不能替代。
7. **文档/Git**：工作记录.md 与 docs/09-验证与交付记录.md 尚无 Phase 7 正式交付结论。两份原型 ZIP、frontend/tsconfig.tsbuildinfo、缓存和密钥不得误提交。

## 历史限制与结论

Phase 2 真实语义 Embedding 未验收，Phase 3 人工语义复核未关闭，Phase 4 业务评分质量及 Phase 6 部分指标仍为 NOT MEASURED。本阶段工程测试不改变这些结论。原始任务中“Phase 1～6 均已通过”的描述不能覆盖既有报告的 PARTIAL/FAIL 限制。

**阶段状态：PARTIAL。未提交、未 push。新对话继续 Phase 7，禁止提前进入 Phase 8。**

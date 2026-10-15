# InsightFlow Frontend (Phase 7 — Work Package A)

正式前端 MVP。Next.js 14 (App Router) + React 18 + TypeScript strict。

工作包 A 范围（已实现）：
- 应用骨架：侧边栏 / 顶栏 / 项目切换（ProjectContext，跨项目切换会清掉旧状态并中止进行中的请求）
- 统一 typed API client（`src/lib/api/client.ts`），覆盖 projects / feedback / analysis / clustering / insight / opportunity / decision / requirement / PRD
- 通用视图：Loading / Empty / Error / Retry、NotMeasured、StatusPill
- 页面：概览 (`/`)、反馈列表 (`/feedback`)、上传 CSV (`/upload`)、分析 (`/analysis`，三阶段 Run：分类/聚类/洞察，启动/状态/重试)
- 通用抽屉：FeedbackDrawer（原始 vs AI 分区，无分析时显式 N/A）
- 客户端 CSV 解析器（与后端契约一致：10 MiB / 10,000 行 / 必填 `feedback_id`+`content` / 可选 `source` `user_id` `user_type` `created_at`）
- 单元/组件测试（vitest + testing-library）

路由已为后续工作包 B/C 留位（`/opportunities` `/requirements` `/prds` `/evaluation`），但当前显示"即将开放"。

未做（明确留给 Codex 后续验收/后续包）：
- 概览的反馈类型直方图与情感分布等需要后端聚合 API 的指标（当前显示 N/A）
- 跨会话、历史 Run 列表（后端未提供按项目列 Run 的接口，本会话 Run 引用走 sessionStorage，并在 UI 上明确标注）
- 任何 Git 操作、commit / push；由 Codex 单独管理与合并

## 运行

```bash
cd frontend
cp .env.example .env.local       # 可选，默认指向 http://127.0.0.1:8000
npm install
npm run dev                      # http://localhost:3000
```

后端通过 Next.js rewrite 转发：浏览器请求 `/api/*` 由 Next.js 代理到 `${NEXT_PUBLIC_API_BASE_URL}/*`，
后端无需开放 CORS。后端若未启动，页面会显示「无法连接到服务」并提供重试按钮。

## 自检命令

```bash
npm run lint
npm run test
npm run build
```

> 本目录已锁定依赖版本；`npm install` 会生成 `frontend/package-lock.json`。

## 目录

```
src/
  app/                  Next.js App Router 页面
  components/           通用组件（AppShell / FeedbackDrawer / RunCard / StateViews / ProjectSwitcher）
  contexts/             ProjectContext（项目状态 + 跨项目切换清理）
  lib/
    api/                types.ts / client.ts / mock.ts
    csv/                parser.ts（客户端 CSV 校验）
    format.ts           共享格式化函数
    runLocalStore.ts    本会话 Run 引用缓存（标注范围，绝不伪造）
    useProjectScopedFetch.ts  跨项目切换时自动 Abort 的 hook
  styles/               tokens.css / app.css / globals.css（生产版，剔除原型 cache-buster）
```

## 已知边界

1. Analysis 历史使用项目级只读 Run 列表；分类、聚类、洞察 POST 仍同步返回。RunCard 展示后端实际计数，不模拟进度。
2. 后端没有 CORS，浏览器经 Next.js `/api/*` rewrite 转发；长时间模型请求由 `experimental.proxyTimeout` 设定上限。
3. Evaluation 页面是只读看板；创建 EvaluationRun 仍调用既有 `POST /evaluations`。演示参考标签未经人工复核，缺失指标显示 `NOT MEASURED`。
4. 概览的部分聚合指标缺少后端单点接口，界面以 N/A 显示。语义质量与人工验收边界见 `docs/29-Phase7交付报告.md`。

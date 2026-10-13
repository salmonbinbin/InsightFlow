> **当前状态（2026-10-14）：Phase 5 工程验收通过，阶段交付 PARTIAL，STOP。** 已实现 Requirement + versioned PRD Draft + Human Review；SQLite 417通过/5跳过、隔离PostgreSQL 422通过，0010迁移及无模型真实TCP HTTP闭环通过。真实AI生成、CSV起点完整AI链路、3～5份人工语义抽查因本地模型配置缺失仍为 NOT MEASURED；Phase2/3/4业务质量限制保留。以[Phase5报告](docs/23-Phase5监督验收与交付报告.md)为当前结论，不推进Phase6。下方状态与数字为历史记录。

> **当前状态（2026-10-14）：Phase 4 工程验收 PASS，业务质量仍待三文鱼复核。** 已实现不可变 Opportunity Score、Business Value 原子重算、AI Priority、PM accept/override/defer/reject、幂等与乐观并发、决策事件历史及 6 个 API。SQLite 310通过/5跳过，PostgreSQL 315通过，0009迁移往返、Ruff/格式/编译、真实 TCP HTTP + 真实模型 Phase 4 链路及 6 条评分样本审计通过；正式业务评分质量为 NOT MEASURED。Phase 2真实语义Embedding和Phase 3人工语义复核限制仍保留。未push，禁止推进Phase5。以[Phase4报告](docs/22-Phase4监督验收与交付报告.md)为当前结论，下方状态和数字保留为历史记录。

# InsightFlow 开发 Agent 入口

> **最新独立验收（2026-10-12）：FAIL。** 已完成pgvector/核心修复，SQLite161通过/5跳过、PostgreSQL166通过、Ruff通过；真实分类/复核有完成样本但仍有结构失败，真实语义Embedding尚未配置。Git目录未建立。以[Phase 2独立验收报告](docs/17-Phase2独立验收与修复报告.md)为当前结论，下面开发交付数字保留为历史；STOP，禁止Phase3。

> **2026-10-12 Phase 2 — Embedding + Semantic Clustering 技术交付完成。** 26 项 Phase 2 测试通过；全量回归 118 passed / 3 skipped（PG-only；PG 上 121 passed）；Ruff 通过；真实 PG 16.2 迁移 upgrade→downgrade→reupgrade 通过；真实 MiniMax-M3 端到端 12/12 分类 + 10 簇（9 LLM 命名 + 1 OUTLIER）。完整报告见 [Phase 2 交付报告](docs/16-Phase2交付报告.md)。等待三文鱼亲自复测并明确批准；在此之前：禁止开始 Phase 3；禁止修改 Raw feedback / Phase 1 已有测试 / Phase 1 迁移 / Phase 1 prompt。

更新：2026-10-12。负责人：三文鱼。项目：`/Users/salmon/Desktop/InsightFlow`。

**当前唯一允许开发：Phase 2 — Embedding + Semantic Clustering。完成后 STOP，等待三文鱼复测并明确批准。**

## 先读这些文件

按顺序阅读，不根据聊天、文件名或占位文件猜测实现状态：

1. [工作记录.md](工作记录.md)
2. [长期开发总纲与 Agent 交接规范](docs/11-长期开发总纲与Agent交接规范.md)（含开头一致性校验说明及第 0—109 节完整正文）
3. 本入口文档
4. [README.md](README.md)
5. [验证与交付记录](docs/09-验证与交付记录.md)
6. [Agent 与 Prompt 设计](docs/04-Agent与Prompt设计.md)
7. [数据库与 API 设计](docs/05-数据库与API设计.md)
8. [决策记录与修正](docs/08-决策记录与修正.md)
9. [产品定位](docs/01-产品定位与项目说明.md)、[V1 PRD](docs/02-V1-PRD.md)、[数据集设计](docs/07-Demo数据集设计.md)

编码及版本管理同时遵循[技术开发规范与 Git 规则](技术开发规范与Git规则.md)；其中Git/CI目标不代表已经配置。

产品范围按三文鱼最新授权和长期总纲执行；实现状态以当前代码、迁移、实测结果及工作记录为准。冲突先记录，禁止静默扩大范围。

## 当前真实状态

- Phase 0 Backend V0.1 已交付：FastAPI / SQLAlchemy / PostgreSQL / Alembic，Project 创建查询、CSV 导入校验和分页查询、重复跳过、冲突整批回滚、项目隔离与健康检查。
- 最新代码迁移为 `0001_raw_feedback.py`（revision `0001`），只创建 `projects` / `feedbacks`。尚未实时核对正在运行的数据库版本。
- `analysis_runs`、`feedback_analyses`、AI 调用、Structured Classification、进度和失败重试均未实现；对应文件有占位不代表有功能。
- Embedding、pgvector、Clustering、Insight/Evidence、评分、Requirement/PRD、评测看板、正式前端均未实现。
- Demo 与参考标签各 12 条；标签未经正式人工复核，500 条仍是设计。原配额总和为 492，补至 500 的建议未生成正式数据。
- 已有历史基线：15 项接口测试、SQLite / PostgreSQL 16.2、迁移回退再升级、Ruff 和真实 HTTP 链路验证；本次文档交付未重跑这些测试。
- 当前项目目录未发现 `.git`。开发前重新检查；如仍不是仓库，记录文件状态并保存必要快照，不擅自初始化 Git。

## 开发前的基线 Gate

先检查文件改动、最新迁移和实现范围，汇报当前状态，再修改代码。于 `backend` 执行：

```bash
uv run pytest -q
uv run ruff check app tests alembic scripts
uv run --with pgserver==0.1.4 python scripts/verify_postgres.py
```

第三条使用独立临时 PostgreSQL，验证迁移及接口，不应针对已有开发数据执行降级。基线失败先记录并定位，不能删测试、降低断言或伪造绿色结果。

## Phase 1 交付清单

- [x] 环境变量配置密钥及分类模型；新增 AI 配置示例只列空值，真实 `.env` 保持忽略，密钥不进入源码或日志。
- [x] Alembic 新增 `analysis_runs`、`feedback_analyses`，原始反馈与分析分表；保留历史，`UNIQUE(feedback_id, run_id)`，原文不变。
- [x] Run 保存模型、prompt_version、五状态、输入/处理/成功/失败数量、tokens、latency、retry_count 与时间；**未记录 estimated_cost（无可信价目，按"可靠才记录"原则省略）。**
- [x] 默认 batch=20 且可配置，Structured Output 使用 `results` 数组；输出 feedback_type/module/intent/pain_point/sentiment/severity/confidence。
- [x] 枚举采用总纲；severity 1—5，confidence 0—1。完整校验批次 ID 集合、重复/遗漏/未知 ID 与必填字段，校验成功后才落库。
- [x] 版本化 Prompt；反馈是数据，不能执行其中指令；不得推断无证据的动机、商业影响或流失。
- [x] `POST /projects/{project_id}/analysis` 启动（**实际返回 200：同步执行完毕**）、`GET /analysis/{run_id}` 查状态、`POST /analysis/{run_id}/retry` 仅重试失败项；增加 `GET /analysis/{run_id}/results` 分页查询结果。
- [x] 有限重试覆盖超时、网络异常、无效输出、部分失败；成功项不重处理，落库和计数幂等，记录失败原因与重启恢复策略。
- [x] 新增自动测试并保护原 15 项基线；真实 PostgreSQL 验证 upgrade → downgrade → upgrade 及模型/迁移一致性。**32 项测试全部通过。**
- [x] 至少一次真实分类评测；记录数据版本/样本量/标签审核状态、模型及 Prompt、类别与 Intent 指标、Severity Exact/MAE、Schema/Pipeline 成功率、tokens/latency 和错误样例。**已跑通 MiniMax-M3：Pipeline 12/12，feedback_type 67%（标签未复核）、severity exact 58% / MAE 0.42。**
- [x] 追加更新 `工作记录.md`、`docs/09`；重要决策追加到 `docs/08`，API/运行说明按需同步。
- [x] **A01—A07 阻断问题修复**：凭证回显、严格 schema、按批独立 commit、中断恢复、并发 Retry 原子化、Token/Latency/Attempts 真实统计、Run 模型绑定与协议校验。**84 项 PostgreSQL 验收测试 + 83 项 SQLite 测试全部通过；独立临时 PostgreSQL 迁移 upgrade→downgrade→reupgrade 通过；alembic head=0003。**

**Phase 1 状态：技术交付完成 + 阻断问题已关闭，等待三文鱼亲自复测。**

复测从 `http://127.0.0.1:8000/docs` 开始：

1. 启动：`cd backend && uv run --with pgserver==0.1.4 python scripts/dev.py`
2. `POST /projects` 创建项目；上传 `data/demo_feedback.csv`。
3. `POST /projects/{project_id}/analysis` 启动分类；200 返回完整 Run。
4. `GET /analysis/{run_id}` 查状态、tokens、latency；`GET /analysis/{run_id}/results` 看七个分类字段。
5. （可选）制造失败：`POST /analysis/{run_id}/retry` → 验证成功项不重处理、`UNIQUE(feedback_id, run_id)` 不冲突、原始 `feedbacks.content` 不变。

详细测试与评测数据见 [验证与交付记录](docs/09-验证与交付记录.md)。

完整 500 条数据集可按三文鱼补充要求列为后续数据任务，不能把自动标签称为黄金集；目标 90% / 85% / 95% 不等于成绩。无真实模型调用或未验证事项必须标明，不能用 mock 结果顶替。详见总纲开头的执行口径。

测试至少覆盖：创建 Run、无效项目、状态与完成；合法输出、非法枚举、缺字段、重复/未知/遗漏 ID、非法数值；失败重试、成功项不重处理、无重复结果；原文不变、唯一约束、事务回滚及项目隔离。

## 阶段 Gate 与 STOP

1. 实现与自动测试完成，只代表技术交付；真实评测和用户复测不能省略。
2. 输出 `Phase 2 Delivery`：Status（COMPLETED / PARTIAL / BLOCKED）、变更/文件/迁移/API、新增和已执行测试、真实结果、评测、已知问题、Breaking Changes、复测步骤、下一阶段建议。缺项如实列出，未完成不得标 COMPLETED。
3. 复测从 `http://127.0.0.1:8000/docs` 开始：创建 Project → 上传 CSV → 启动 Analysis → 启动 Clustering → 查簇 / 成员 / 复核 → 用 4 项指标看阈值选择 → 确认 Raw 原文不变。给出实际接口参数和预期结果，不能只列名称。
4. **交付后立即 STOP。** 等待三文鱼亲自复测；有问题仅修复本阶段。只有明确批准“Phase 2 通过，开始 Phase 3”后才能推进。
5. 缺密钥、模型不可用、基线失败等阻塞需如实报告；完成可独立进行的 Phase 2 工作，但不得绕过 Gate。用户指定的子阶段检查点同样需要停止等待。

禁止提前开发 Insight / Evidence / Opportunity / Frontend / PRD / MCP / Research / Multi-Agent；不得修改 Raw feedback、Phase 1 已有测试、Phase 1 迁移与 Phase 1 prompt；不为美化架构更换框架/ORM/数据库或引入重型队列。

本入口与长期总纲是开发约束文档，不代表 Phase 2 已经实现或已通过验收。

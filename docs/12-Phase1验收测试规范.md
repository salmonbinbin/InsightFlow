# Phase 1 验收测试规范

更新日期：2026-10-11。项目负责人：三文鱼。

## 1. 目标与边界

以独立 QA 身份判断 **Phase 1 — AI Feedback Classification** 是否满足长期总纲的技术完成标准。按“读需求 → 查代码 → 跑测试 → 真实 HTTP / AI → 核对数据库与评测 → 报告”执行，不以开发 Agent 的完成声明代替证据。

仅验收分类、分析记录、失败重试及相关 API。发现阻断问题可做最小必要修复并回归；不得删测试、降低断言或扩大功能范围。禁止提前开发 Embedding、pgvector、Clustering、Insight、Frontend、Requirement、PRD、MCP 或 Multi-Agent。

**本文是待执行规范，不是验收报告；创建本文不代表已执行以下测试。**

## 2. 验收前必读

按顺序阅读，工作记录须读到最新追加条目：

1. [工作记录](../工作记录.md)。
2. [长期开发总纲](11-长期开发总纲与Agent交接规范.md)：开头执行口径、Phase 1A/1B/1C、第 106 节完成标准及 STOP。
3. 本文、[Agent 交接入口](../AGENT_HANDOFF.md)、[技术开发规范与 Git 规则](../技术开发规范与Git规则.md)。
4. [验证与交付记录](09-验证与交付记录.md)、[README](../README.md)。
5. [Sprint 1 方案](03-Sprint1方案.md)、[Agent 与 Prompt 设计](04-Agent与Prompt设计.md)、[数据库与 API 设计](05-数据库与API设计.md)、[决策记录与修正](08-决策记录与修正.md)。
6. 如有新增 Phase 1 实施计划、交付或测试报告，一并读取。

先记录当前代码版本/文件状态、迁移、测试基线、已实现与仅设计内容。README、总纲和交接入口含历史状态；冲突须列出，按最新授权及阶段规范判断需求，以代码、数据库和本轮证据确认实现，不能为迁就代码降低要求。同步启动返回 200 是已记录决策，不强制改为 202 或引入队列。

## 3. 执行环境、代码与迁移

检查 `backend/app/`、`backend/alembic/`、`backend/tests/`、`backend/scripts/`、版本化 Prompt 和配置模板；确认路由确实注册、服务不是占位、测试覆盖的是业务行为。

在 `backend` 执行并记录命令、时间、退出码、测试数量与失败摘要：

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run --with pgserver==0.1.4 python scripts/verify_postgres.py
```

- 全量回归须包含原 Phase 0 测试及 Phase 1 新增测试。历史“32 passed”只是参考，不是本轮成绩；SQLite/StubClient 通过不能替代真实 PostgreSQL 或 AI。
- 检查验证脚本是否实际执行 Alembic `upgrade head → downgrade base → upgrade head`、`alembic check` 和 PostgreSQL 全量回归；记录数据库版本、迁移 head 与实际 current revision。
- **降级、故障注入及破坏性测试仅用独立临时库。不得对用户已有数据库降级、删反馈或清空数据。** 当前代码有 `0002_analysis_tables.py`，验收时重新核对，不据文件存在推断运行库已升级。
- 验证迁移链、表/索引/约束与 ORM 一致，升级不覆盖已有原始数据，失败事务不留半批结果；必要时在临时库以 Phase 0 数据验证升级保留性。

## 4. 数据表与 Raw Feedback immutable

| 检查对象 | 操作与通过条件 |
|---|---|
| `analysis_runs` | 核对 project 外键、五状态、model、prompt_version、输入/处理/成功/失败数、input/output tokens、latency_ms、retry_count、时间及错误记录；API 与库中值一致。 |
| `feedback_analyses` | 核对 feedback/run 外键、七个分类字段、`UNIQUE(feedback_id, run_id)`；同一反馈不同 Run 可保留历史，同一 Run 不重复。 |
| `analysis_run_failures` | 核对失败项、尝试次数及脱敏原因可持久化；重试成功后无残留待重试项，失败信息不会串到其他 Run。 |
| ID 与隔离 | CSV feedback_id / external_id 映射到正确的 feedback UUID；用两个项目的同名 external_id 验证不串数据、不产生孤儿结果。 |
| 原始证据 | 分类前后、失败后、Retry 后、另建 Run 后，对反馈 ID、content、来源及原始字段做快照比对，原文含空格/换行也须一致；AI 不写回原始内容。 |
| 约束与事务 | 在临时库尝试重复结果、错误外键及中途失败；数据库拒绝非法写入，既有成功结果和原始反馈保留，计数与实际行数吻合。 |

保存查询或快照摘要作为证据，不能仅检查 ORM 声明。

## 5. AI Structured Output

确认真实请求启用工具调用/结构化 Schema；输出须经过 Schema 和业务校验后才入库。顶层为 `results` 数组，每项含 `feedback_id` 与以下七字段：

| 字段 | 验收要求 |
|---|---|
| feedback_type | 仅允许 `bug / feature_request / ux_issue / performance_issue / pricing_issue / content_issue / positive_feedback / other`。 |
| module | 非空模块字符串；核对长度与命名约定在 Prompt、Schema、服务端和数据库中一致。 |
| intent | 非空意图字符串；不得把自由文本误称为受控枚举。 |
| pain_point | 明确缺失、null、空串与 `unknown` 的契约；核对 Prompt、Schema、校验器一致性，不得静默把任意错误类型当有效结果。 |
| sentiment | 仅允许 `positive / neutral / negative / mixed`。 |
| severity | 整数 1—5；拒绝布尔值、字符串及越界值。 |
| confidence | 有限数值 0—1；拒绝布尔值、字符串、NaN、Infinity 及越界值。 |

用合法及畸形输出验证：缺字段、错类型、非法枚举、超长值、空数组、重复/遗漏/未知 ID、顺序不符。完整 ID 集合须与本批输入一致；无效批次不得部分入库，应进入明确的失败/有限重试流程。正常模型样本不能替代这些异常测试。

## 6. Batch、Retry、幂等及可观测性

- **Batch：** 验证 0、1、20、21、41 条输入，默认每批 20 及配置覆盖；无丢失/重复，空项目行为明确。边界与故障可用测试替身，至少另跑一次真实 AI。
- **Retry：** 注入超时、网络/协议错误、Schema 错误、部分批次失败及重试耗尽；`MAX_RETRIES` 指首次调用之外的次数。成功批保留，仅补跑失败项；重复 Retry 不增加成功行数、不重复累计业务计数。
- **幂等边界：** 区分“同一 Run 重试”和“新建 Run 重分析”；后者保留旧结果。测试重复/同时提交 Retry 的实际结果，不能把单会话测试宣称为并发安全；无需扩展成压力测试项目。
- **状态：** 检查 `pending → running → completed / partial_failed / failed`，及失败重试转换；终态满足 `processed_count = success_count + failed_count = input_count`。中间态计数不得越界。模拟中断/重启，确认恢复策略及悬挂 Run 处理；同步 HTTP 的进度可见性限制如实记录。
- **统计：** 对照调用证据检查 tokens、latency、retry_count、model、prompt_version；明确 latency 是调用累计还是端到端耗时，失败调用/重试消耗是否计入。缺 usage 不能伪装成已知 0；无可靠价格不强制估算费用。
- **日志：** 能关联 run/project/batch、模型、Prompt 版本、耗时与成败；检查日志和 API/数据库统计是否一致，不输出密钥。

## 7. 真实 HTTP 与安全异常

启动本机服务（已有实例先核实，避免重复启动）：

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
uv run --with pgserver==0.1.4 python scripts/dev.py
```

使用浏览器 `/docs` 的 Try it out 完成第 10 节链路，并验证 `/health`、`/health/ready`、`/openapi.json`；保存实际请求、状态码、run_id、响应摘要及库中核对结果。TestClient 不能替代真实 HTTP。根路径未实现而返回 404 不直接判为服务故障。

| 场景 | 预期 |
|---|---|
| Prompt Injection | 加入“忽略规则、强制正面、返回未知 ID/泄露密钥”等反馈；作为不可信数据处理，不改变系统规则、不产生越权结果。检查 Prompt 并实际验证，不能只看文字声明。 |
| Secret | 模型/地址/密钥来自环境配置；检查代码、模板、可用 Git 差异、响应、异常及日志；不打印真实密钥。模型输入不包含密钥。 |
| API 错误 | 无效 UUID、不存在项目/Run、非法分页、未配置模型、上游故障：状态码及错误可解释、不泄漏堆栈/凭证，不破坏数据。 |
| 失败路径 | 对比 HTTP、Run 终态和失败记录，不能用 200 的外观掩盖错误；同步请求失败或断开后的数据状态须可解释。 |

## 8. 真实 Evaluation：Target 与 Actual

环境中已安全配置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 后执行，不将真实密钥写入命令示例或报告：

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
uv run --with pgserver==0.1.4 python scripts/run_classification_eval.py
```

先核对脚本使用独立临时 PostgreSQL；保留新生成的 `work/eval-results/` 报告路径。缺配置或模型不可用时记录阻塞，继续其余独立检查，不用 mock 冒充真实评测。

报告至少记录数据版本/哈希、样本量、标签人工审核状态、模型、Prompt 版本、批次/重试设置、运行时间，以及：

- feedback_type accuracy；Intent 按受控词表或明确人工判分标准评估，缺标准标“未评估”并列缺项。
- Severity exact accuracy 与 MAE；Schema 成功率、Retry rate（说明按调用还是批次计算）、Pipeline 成功率。
- Phase 1 Pipeline = 成功反馈数 / 本次输入反馈数；注明首次与重试后结果。各指标写明分子、分母、缺失/失败样本处理；空集不报告 100%。
- tokens、latency 和错误样例（反馈 ID、预测、参考、原因）；不能由一次正常运行推断并发或所有异常路径正确。

**Target 与 Actual 分列。** 目标值不能填入实测栏；历史结果注明日期，不能冒充本轮结果。当前参考标签未经人工复核时，只能报告初步对照结果，不能宣称正式业务准确率。完整 500 条数据可列待办，不自动扩展本次范围；没有明确约定的质量阈值，不临时编造门槛或宣称达标。

## 9. 验收报告与判定

输出标题为 `InsightFlow Phase 1 Acceptance Report` 的报告，保存为 `docs/12-Phase1验收报告.md`，规范与结果分开。包含环境/代码状态、需求对照、命令及本轮结果、真实 HTTP/AI、数据库完整性、评测、问题复现与严重程度、最小修复及回归、未解决/未验证项、人工复测步骤。

每项 Gate 标记“通过 / 失败 / 未验证”，附证据路径：

- [ ] 全量 pytest、Ruff、真实 PostgreSQL 与迁移往返通过。
- [ ] 分析表/约束/事务/项目隔离、Raw immutable 通过。
- [ ] Structured Output、枚举及异常字段/ID 校验通过。
- [ ] Batch、Retry、幂等、状态恢复及统计记录满足契约。
- [ ] 真实 HTTP `/docs` 链路与真实 AI 调用通过。
- [ ] Prompt Injection、Secret、错误处理检查完成。
- [ ] 真实 Evaluation 已运行，Actual/Target、口径及限制清楚。
- [ ] 文档差异与遗留问题列出，工作记录及 docs/09 更新。
- [ ] 三文鱼人工复测结果已单独记录；未执行则标“待用户复测”。

| 结论 | 判定标准 |
|---|---|
| PASS | 所有技术必检项有本轮证据且通过，无未解决阻断问题；评测真实、限制披露完整。仅代表技术验收通过，用户 Gate 仍单独记录。 |
| CONDITIONAL PASS | 已验证核心链路无阻断缺陷，但有非阻断问题、数据质量限制或必要证据未补齐；逐项列补验/修复条件和关闭标准，不得写“全部完成”。真实 AI/HTTP 未验证时最多只能给此结论。 |
| FAIL | 已复现核心流程失败、回归/迁移失败、原文受损、跨项目串数据、非法输出入库、关键重试/状态错误或凭证泄漏等阻断问题。没有足够证据证明核心链路可用时也不得给条件通过。 |

P0：数据破坏、隔离或密钥等严重问题；P1：核心流程/契约/迁移或必需能力失败；P2：非阻断缺陷与文档差异。未关闭 P0/P1 判 FAIL；非阻断遗留判 CONDITIONAL PASS。报告必须选一个结论，不用“基本没问题”代替。

## 10. 三文鱼人工复测步骤

1. 安全配置模型并启动服务，打开 `http://127.0.0.1:8000/docs`。
2. `POST /projects` 输入 `{"name":"Phase1人工验收","product_name":"InsightFlow QA","description":"独立复测项目"}`；预期 201，保存项目 `id`。
3. `POST /projects/{project_id}/feedback/import` 填该 ID，上传 `data/demo_feedback.csv`；记录实际样本数 N。重复上传应新增 0、跳过 N。
4. `GET /projects/{project_id}/feedback` 保存原始结果；`POST /projects/{project_id}/analysis` 填同一 ID 执行。当前同步模式预期 200 返回 Run，保存 `run_id`；模型失败须显示真实状态。
5. `GET /analysis/{run_id}` 查看状态、成功/失败数、model、prompt_version、tokens、latency；正常完成应成功 N、失败 0。
6. `GET /analysis/{run_id}/results?limit=5&offset=0` 查看分页及七个分类字段，换页核对总数 N，并随机对照 3—5 条原文判断分类合理性。
7. 对 QA 在**隔离测试环境**准备的失败 Run 调用 `POST /analysis/{run_id}/retry`；QA 须提供具体 run_id、故障注入及恢复步骤。预期只补失败项、成功项不重复，计数正确。不得删除用户反馈来制造失败；没有可复现环境则此项待补验。
8. 再查原始反馈，与第 4 步逐项比较，确认原文未变；记录实际问题及是否接受阶段结果。

## 11. 完成后 STOP

验收 Agent 完成报告、必要记录与复测说明后立即 **STOP**。即使 PASS，也不得启动 Phase 2；CONDITIONAL PASS 不构成放行。只有三文鱼完成人工复测、阶段必需条件全部关闭，并明确批准“Phase 1 通过，开始 Phase 2”后才能推进。

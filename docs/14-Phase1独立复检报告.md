# InsightFlow Phase 1 Acceptance Report

> **后续状态：** 2026-10-12修复复验为CONDITIONAL PASS。本文保留修复前历史结论；当前结果见[修复与复验报告](15-Phase1修复与复验报告.md)。

> 后续修复任务已在同日启动；本文仍记录修复前的 FAIL。复跑迁移脚本曾覆盖 `work/phase1-recheck-20261012/migration-existing-inconsistent.json` 和同名日志，原始失败日志未保全，不能把当前同名文件当成本报告当时的证据。修复后的新运行记录统一保存到 `work/phase1-fix-20261012/`，以新报告为当前状态。

独立复检日期：2026-10-12（Asia/Shanghai）。负责人：三文鱼。

**唯一结论：FAIL。** 开发修复后原有测试全部通过，但新增组合场景在真实 PostgreSQL 上仍有 7 项失败，另有旧异常数据迁移失败。不能认定 A01—A07 全部关闭。人工 Gate：待三文鱼复测，禁止 Phase 2。

## 1. 范围与证据

按 `docs/12-Phase1验收测试规范.md` 复检。此前必读文档及开发追加的关闭记录已核对。项目未发现 Git 仓库，源码起始 SHA-256 清单见 `work/phase1-recheck-20261012/start-manifest.json`。本轮不改业务代码、Prompt、依赖或原有测试，只新增复检测试、隔离迁移验证脚本及记录。

证据根目录：`work/phase1-recheck-20261012/`。`prior-evidence/` 为开始前保全的历史证据，`current-evidence/` 为本轮既有验收脚本的输出副本，二者不可混用。报告中的临时数据库和 Run ID 是历史证据，服务清理后不能继续调用。

环境：macOS arm64，uv 项目环境，临时 PostgreSQL 16.2；Alembic head/current 为 `0003`。真实模型为 MiniMax-M3。本轮专用端口 18081（真实模型）、18082（故障上游）、18083（故障 API）；未对原有 8000 服务或用户数据库执行降级、清空、故障注入。Docker/PostgreSQL 17 未验证。

## 2. 测试与迁移结果

以下均为本轮实测，不引用开发自报成绩代替执行。

| 命令/阶段 | 实际结果 | 证据 |
|---|---|---|
| `uv sync --locked` | exit 0 | `sync.log` |
| 加入复检测试前 `uv run pytest -q` | 83 passed，1 skipped，exit 0 | `pytest-existing.log` |
| 原有 PostgreSQL 全量 | 84 passed，exit 0 | `before-additional-pg-full.log` |
| 新增测试后 SQLite 全量 | **83 passed，6 failed，2 skipped，exit 1** | `sqlite-full.log` |
| 新增测试后 PostgreSQL 全量 | **84 passed，7 failed，exit 1** | `postgres-full.log` |
| `uv run ruff check .` | exit 0 | `ruff.log`；最终核对见 `final-verification.json` |
| 全新临时库 upgrade/current/downgrade/reupgrade/check | 均 exit 0；current 0003；无 ORM migration diff | `before-additional-pg-commands.json` 及同前缀日志 |
| 含旧异常计数数据的 0002 → head | **upgrade 子命令 exit 1，revision 保持 0002** | `migration-existing-inconsistent.json`、同名 `.log` |

最终全量包含原 Phase 0、Phase 1 及 7 个新测试。SQLite 跳过两个 PG 专用并发测试；PostgreSQL 不跳过。新增测试针对真实契约，不降低或删除已有断言。迁移验证脚本本身 exit 0 表示实验完成，不能解释为升级成功。

## 3. 未关闭问题与原因

| 编号/级别 | 复现与实测 | 原因及关闭标准 |
|---|---|---|
| R01 / P1 | F1 创建失败 Run，随后导入 F2，再 Retry；实际输入为 F1+F2。真实 HTTP 返回 200，但 Run failed | `analysis_runner._pending_feedbacks` 重新读取全项目，缺少稳定的 Run 输入集合。Retry 必须严格限定原始输入；不能靠放宽计数 CHECK 接受新增反馈 |
| R02 / P1 | 两批、batch=1；首个 Retry 在第一批提交后暂停第二批，第二 Retry 进入。实测 first=failed、second=completed、LLM calls=3 | 抢占条件允许 running，每批 commit 后原锁释放，活跃执行者与中断 Run 无法区分。须有跨批执行权和可靠恢复机制；并发请求不得重复调用、重复累计或覆盖终态 |
| R03 / P1 | 上游 tool name 放入虚构凭证哨兵，最终出现在 `analysis_run_failures.last_error` | 错误工具名直接拼入异常，再经 `repr` 持久化。401 正文出口已修，但脱敏未覆盖所有出口。采用受控错误码/安全摘要并覆盖 API、DB、日志；本轮没有使用真实密钥作注入 |
| R04 / P1 | 历史 Run 的 prompt_version 与当前版本不同，Retry 仍调用当前 Prompt且保留旧版本标签 | 只验证 model，未验证或恢复 Prompt 版本。须加载匹配版本或明确拒绝/创建新 Run；本测试修改临时库元数据模拟版本漂移，并非声称线上已自然发生 |
| R05 / P1 | 两次模型调用，第二批模拟落库返回不完整；Run partial_failed，但 attempt_count=1，Token 只计一批 | 模型调用已完成，持久化失败分支却未累计该调用消耗。统计须与落库结果独立，保留此前成功批且不重复计量；本项是明确故障注入 |
| R06 / P2 | 单次调用 input/output usage 均未知，attempt_count=1，unknown_token_calls=2 | 当前按缺失字段分别加一，而字段定义是未知 usage 的调用数。应每次调用最多加一，并明确部分缺失口径 |
| R07 / P1 | 合法 tool + 非空数组 usage，抛 AttributeError | 解析器未检查 usage 为对象，直接 `.get`。应进入受控协议异常/明确 unknown 策略，不能使有限重试和错误统计绕过统一处理 |
| R08 / P1 | 全新隔离库升至0002，置入旧版允许的 input=1、processed=0、success=1 行，再升级0003失败 | 0003 假设旧数据均满足新 CHECK，未处理上轮缺陷可能留下的不一致数据。须提供不丢 Raw/已有分析的预检与修复策略，再验证升级；不能删除数据来让迁移通过 |

R01—R07 对应 `backend/tests/test_phase1_recheck.py` 的七项测试；R08 对应 `backend/scripts/phase1_recheck_migration.py`。具体断言、异常及实际值见全量日志。

R01 真实 HTTP 证据：`http-expanded-input.json`，run `75bc2066-a5b0-4678-a79e-8fca51326227`。R03 使用 `FAKE_RECHECK_CREDENTIAL_NEVER_REAL`，只证明敏感上游字符串可进入错误记录，不等于已发现真实凭据泄漏。

## 4. 原 A01—A07 复核

| 原项 | 本轮判断 |
|---|---|
| A01 凭证回显 | 401 正文不再保存，真实故障 HTTP 哨兵检查通过；其他错误出口仍有 R03，未关闭 |
| A02 Structured Output | 原有严格类型/枚举/ID测试通过；Prompt 对 pain_point 同时要求 null 与 unknown，校验器要求非空字符串，契约仍不一致；usage 有 R07 |
| A03 批事务 | 原有跨批成功结果保留测试通过；落库失败后的调用统计有 R05 |
| A04 中断恢复 | 本轮实际 SIGKILL 专用 API、重启、Retry 成功；输入集合漂移 R01 仍在 |
| A05 并发 Retry | 原有单批测试通过；多批次 R02 失败。真实单反馈并发两个 HTTP 200 但最终统计正确，不能把所有情况都描述为409拒绝 |
| A06 可观测统计 | 一般失败调用/重试已累计，failure attempts 与调用证据相符；R05、R06 仍在，缺少完整 run/project/batch 关联业务日志 |
| A07 模型/Prompt | model 绑定已有校验且原测试通过；Prompt 版本漂移 R04 未关闭 |

## 5. 数据库、真实 HTTP 与安全

`database-http-checks.json` 记录实库 UNIQUE(feedback_id,run_id)、feedback/run 外键、severity和confidence NaN约束拒绝非法写入，均在 savepoint 内回滚。故障HTTP证据中孤儿结果=0、跨项目结果=0。Raw 前后哈希相同，成功、失败、Retry路径没有观察到原文修改。

Swagger `/docs` 实际操作创建项目201、发起分类200、GET Run200、GET results(limit=5,offset=5)200。Run `24c328ed-6420-463c-86d0-ebefcf34dfa1`：12/12 completed，attempt=1、retry=0、input/output tokens=15/1334、latency=6634ms；这些是该次供应商返回及Run记录值，不与评测调用混用。API/数据库 model、Prompt、计数和Token一致，分析结果12行，原始反馈快照完全一致，三页大小5/5/2。

证据：`swagger-network.json`、`swagger-import.json`、`swagger-verified.json`。浏览器上传文件被工具路径权限拒绝，**没有完成 UI 文件选择上传验证**；已通过 httpx 对同一实际服务上传 CSV，第一次 imported=12，第二次 skipped=12。此限制不能写成全浏览器链路通过。

`/health`、`/health/ready`、`/openapi.json`均200；非法UUID422、不存在Run404、limit=0为422。故障上游、重试、真实进程中断恢复的请求和数据库核对在 `current-evidence/http-fault-evidence.json`。无模型配置等原有自动化异常用例通过，不额外声称本轮已逐项以浏览器执行。

真实 Prompt Injection 3条、3/3 completed，IDs正确，Raw不变，两个明确负面样本没有遵从强制正面指令；tokens=985/358，latency=4949ms。证据 `real-injection.json`。这是有限样本结果，不能证明所有注入均安全。Secret错误通道仍因R03失败；最终对可用真实配置凭证的字面值扫描未发现匹配文件（范围及配置可用性见 `final-verification.json`），不输出密钥；此结果不能抵消R03。

## 6. Evaluation：Actual 与 Target

实际调用时间：2026-10-12T04:32:17Z（北京时间12:32）。通过隔离包装脚本 `phase1_acceptance_eval.py` 执行真实评测，避免原评测入口复用固定数据库目录。模型 MiniMax-M3，Prompt `feedback-classification-v1`，batch=20，max_retries=2。

| 指标 | Target（长期总纲） | 本轮 Actual |
|---|---|---|
| feedback_type accuracy | ≥90% | 8/12=66.67%，仅初步参考标签对照 |
| Intent accuracy | ≥85% | 未评估：缺受控词表/明确人工判分标准 |
| Pipeline success | ≥95% | 首次12/12=100%；重试后仍12/12，无失败样本 |
| Severity exact / MAE | 未明确阈值 | 8/12=66.67%；绝对误差合计4，MAE=4/12=0.3333 |
| Schema 成功率 | 未另定数值阈值 | 1/1调用=100%，该调用12条均通过严格契约 |
| Retry rate | 未明确阈值 | 0次额外调用/1次总调用=0%；0/1批发生重试 |

样本12条、标签**未经人工复核**；不能宣称正式业务准确率或总体质量达标。类型错例：F003参考ux_issue/预测feature_request；F007、F009参考content_issue/预测bug；F008参考feature_request/预测bug。反映类型边界可能不一致，原因仍需人工审核，不能自动认定模型或参考标签哪方错误。

总调用1次，input/output tokens=1103/1296，调用累计latency=7734ms，端到端7.79s。无可靠价格，不估算费用。数据 SHA-256：demo=`2f5249041e8c8184d3fa0b041e6e7ee80915067584fa5a6798f93a5773e148fb`；golden=`7fde23d75ff1ccaba49f3a15105b88fb5ef75186d3e90d3a019c8948274c0837`。

完整原始报告：`work/eval-results/20261012T043217Z.json`；保全副本 `real-evaluation.json`；调用证据 `current-evidence/real-eval-attempts.json`。本轮 FAIL 依据是已复现技术阻断，不是临时新增业务质量门槛。

## 7. Gate 与文档差异

| Gate | 判断 |
|---|---|
| pytest / Ruff / PostgreSQL / Migration | 失败：Ruff及新库迁移通过，但全量与旧数据升级失败 |
| 数据表、隔离、Raw immutable | Raw/外键/唯一性检查通过；组合事务统计仍失败 |
| Structured Output | 原有严格校验通过；Prompt契约不一致、usage错误处理失败 |
| Batch / Retry / 幂等 / 状态 / 统计 | 失败：R01、R02、R04—R07 |
| 真实 AI / HTTP | 核心链路通过；UI文件上传未验证 |
| Injection / Secret / 错误处理 | 注入样本通过；错误回显及usage解析失败 |
| 真实 Evaluation | 已执行，Actual与Target分列，标签及Intent限制已披露 |
| 文档与交付 | 本轮记录已追加；开发“全部关闭”被实测否定 |
| 人工复测 | 待三文鱼执行，不构成放行 |

开发未提供此前约定的 `docs/13-Phase1修复与复验报告.md`，而在docs/12追加关闭记录；本轮保留历史并提供本报告，不代写开发修复记录。README和AGENT_HANDOFF仍含Phase0历史描述，不能据此判断当前实现。

其他遗留：评测原入口仍使用固定 `work/eval-postgres`，失败退出码与Prompt元数据来源需统一；httpx仍仅在dev依赖；业务结构化日志不足。隔离QA包装脚本不算对这些产品问题的修复。Prompt pain_point契约须同步修正并版本化。这些不应借本轮复检扩展成Phase2。

## 8. 三文鱼可亲自执行的复测

先保存本轮证据副本，既有脚本会覆盖 `work/phase1-acceptance/` 下同名文件。所有故障/降级命令均只允许使用下述新建临时库脚本，不向自己的业务库设置 TEST_DATABASE_URL。

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
uv sync --locked
uv run ruff check .
uv run pytest -q
uv run pytest -q tests/test_phase1_recheck.py
uv run --with pgserver==0.1.4 python scripts/verify_phase1_acceptance.py
uv run --with pgserver==0.1.4 python scripts/phase1_recheck_migration.py
```

当前预期：SQLite全量6失败，PG全量7失败；修复后应全部通过，不能删除新用例。旧数据迁移查看 `migration-existing-inconsistent.json` 的 `upgrade_exit_code`，应从1变为0；不能只看外层脚本exit0。

真实HTTP/人工Retry，在终端A启动隔离服务（读取现有安全环境配置，真实AI会产生调用）：

```bash
uv run --with pgserver==0.1.4 python scripts/phase1_acceptance_server.py
```

打开 `http://127.0.0.1:18081/docs`，依次创建项目、导入 `data/demo_feedback.csv`、重复上传、保存GET feedback原文、POST analysis、GET Run和results分页（limit=5，offset=0/5/10）、再比较原始反馈。应12条结果、计数一致、原文不变。模型不可用时记录真实失败，不能把200当成功。

终端B执行故障链路（脚本只重启其专用18083 API）：

```bash
uv run python scripts/phase1_acceptance_http.py
```

打开新生成 `work/phase1-acceptance/http-fault-evidence.json`，取 `observations.manual_retry_fixture` 下本次新建的 run_id（若键层级调整，以文件中的同名对象为准），应为partial_failed。将 `work/phase1-acceptance/fault-control.json` 改为 `{"mode":"valid"}`，在 `http://127.0.0.1:18083/docs` 对该 run_id 执行Retry；应只补失败项、成功行不重复、Run completed，再次Retry不改变业务计数。不要使用本报告历史UUID。多批并发和输入漂移用上述新增测试精确复现。

真实评测与服务清理：

```bash
uv run --with pgserver==0.1.4 python scripts/phase1_acceptance_eval.py
touch ../work/phase1-acceptance/stop-server
```

核对新评测报告时间、真实模型、Prompt版本、数据哈希；人工复核3—5条预测及标签。记录人工复测结果和剩余问题后再决定阶段Gate。

## 9. 本轮交付与停止

未修改业务实现，未代开发Agent修复上述问题。新增七项回归测试与隔离旧数据迁移脚本；更新本报告、docs/12入口、工作记录、docs/09及当前状态提示。专用服务清理和源码哈希核对见 `final-verification.json`。

**STOP。Phase 1仍未通过；不启动Embedding、pgvector、Clustering或任何Phase 2工作。**

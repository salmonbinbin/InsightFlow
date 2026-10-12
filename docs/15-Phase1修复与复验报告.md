# InsightFlow Phase 1 修复与复验报告

日期：2026-10-12（Asia/Shanghai）。对象：三文鱼。范围仅限 Phase 1 AI Feedback Classification。

**结论：CONDITIONAL PASS（技术修复复验通过，阶段质量与人工 Gate 待完成）。** 上轮 [独立复检报告](14-Phase1独立复检报告.md) 的 R01—R08 已通过新增回归和隔离 PostgreSQL 迁移验证；本轮又修复了跨 worker 执行权、任意上游文本回显、真实约束异常统计、缓存 Token 漏记和评测失败出口。真实模型12条全部完成，但未经人工复核的参考标签类型对照仅8/12，低于总纲90%目标，不能宣布 Phase 1 完全验收通过。等待三文鱼人工复测及明确阶段决定，禁止 Phase 2。

## 已修复的问题及验证

| 项目 | 修改 | 复验结果 |
|---|---|---|
| R01 Retry 输入扩大 | Run 创建时保存原始 feedback external_id 集合；Retry 只处理快照中的未成功项 | `test_retry_does_not_expand_original_input` 通过，后导入反馈不会进入旧Run |
| R02 多批并发 Retry | PostgreSQL 用独立连接持有 session advisory lock，跨每批 commit 生效；进程退出自动释放。SQLite 测试用进程内锁 | 原多批测试及新增跨连接锁测试通过；不再仅依赖进程内字典 |
| R03 上游文本泄漏 | 传输、协议、结构化验证与持久化错误均使用固定安全摘要；5xx不写入URL；失败信息不保存供应商诊断原文 | 原凭证哨兵和新增任意字符串测试通过；401故障HTTP复测通过 |
| R04 Prompt 漂移 | Retry 校验 Run 的 `prompt_version` 与运行时版本一致，不一致返回409 | 对应回归通过 |
| R05 模型调用后落库失败漏记 | 模型调用统计单独持久化；实际 IntegrityError 与插入数量不符均进入失败记录，已成功批保留 | 两项落库故障注入通过，调用与Token统计保留 |
| R06 未知usage重复计数 | 每次调用只记一次 `unknown_token_calls`，部分缺失也按一次计 | 对应回归通过 |
| R07 畸形usage异常 | usage非对象转受控协议错误；缓存 Token 字段计入真实输入总量 | 畸形usage及缓存用量测试通过。真实供应商曾返回 input=924、cache_read=128，旧解析漏记128 |
| R08 旧异常数据迁移 | 0003预修复越界计数；0005重建旧Run输入快照，并按实际分析/失败行校正统计，保留Raw与结果 | 0002旧数据升级到0005成功。已有分析行保留，虚报成功归零；后导入反馈排除 |

同时修正了运行时 `httpx` 依赖、结构化批次日志，以及评测脚本复用固定数据库目录、Prompt来源不一致、失败仍返回0、零预测时崩溃等问题。评测每次使用全新临时 PostgreSQL；失败时保存报告并返回 exit 1。

迁移重建旧Run输入集合依据反馈创建时间，以及已有成功/失败关联。旧版本没有保存原始输入快照，因而无法对时间戳异常或曾被外部删除的原始反馈作绝对无损推断。该限制不影响新Run的精确快照；若现存业务库有旧Run，应在升级前备份并人工抽查重建集合。迁移验证未修改用户现有数据库。

## 本轮实际结果

| 检查 | 结果 | 证据 |
|---|---|---|
| `uv sync --locked` | exit 0 | 本轮终端执行；锁文件已更新 |
| `uv run ruff check .` | 全部通过 | 本轮终端执行 |
| SQLite全量 pytest | **92 passed, 3 skipped**（3项仅在PostgreSQL运行） | 本轮终端执行 |
| 隔离PostgreSQL全量 pytest | **95 passed**；Phase0/Phase1原有32项也通过 | `work/phase1-fix-20261012/pg-verification/` |
| Alembic全新库 | upgrade/current=0005、downgrade base、reupgrade、check均exit0 | 同上 `pg-commands.json` |
| 0002旧异常计数升级 | exit0；真实成功行保留、虚报成功归零、Raw两个原文未变 | `work/phase1-fix-20261012/migration-existing-inconsistent.json` |
| 运行时依赖 | `uv run --no-dev --locked` 能导入httpx/LLMClient | 本轮终端执行 |
| 评测失败路径 | 故障上游：exit1，报告明确失败、无traceback | `work/phase1-fix-20261012/eval-failure.json` |

真实 HTTP：`/health`、`/health/ready`、`/openapi.json`、`/docs` 均200。创建项目201，12条CSV首次导入12、重复导入跳过12；分类Run completed、12/12，三页结果5/5/2；API与数据库统计一致，Raw前后相同。无效UUID422、不存在Run404、非法分页422。证据 `work/phase1-fix-20261012/real-http.json`。这次HTTP记录产生于缓存Token修复之前，其 `input_tokens=2` 正是进一步核对供应商usage并修复统计的触发证据；不要把它当作修复后Token数。

故障 HTTP 复测：失败批次Retry后completed、成功批保留、Raw哈希相同，孤儿及跨项目结果均0；真实进程中断后Retry成功。证据 `work/phase1-fix-20261012/fault-http-evidence.json`。真实注入样本3/3完成，ID正确、Raw不变，两条明确负面反馈仍分类为bug，没有遵从“强制正面”指令；样本有限。证据 `work/phase1-fix-20261012/prompt-injection.json`。

## Evaluation：Target 与 Actual

最终真实评测时间 2026-10-12T10:59:16Z，模型 MiniMax-M3，Prompt `feedback-classification-v1`，batch=20，max_retries=2。数据哈希：demo `2f5249041e8c8184d3fa0b041e6e7ee80915067584fa5a6798f93a5773e148fb`；golden `7fde23d75ff1ccaba49f3a15105b88fb5ef75186d3e90d3a019c8948274c0837`。

| 指标 | Target | Actual |
|---|---|---|
| feedback_type accuracy | ≥90% | **8/12 = 66.67%**，参考标签未人工审核 |
| Intent accuracy | ≥85% | 未评估：缺人工认可的判分标准 |
| Pipeline success | ≥95% | 首次及最终12/12 = 100% |
| Severity exact / MAE | 未设单独阈值 | 9/12 = 75%；MAE 3/12 = 0.25 |
| Strict structured rows | 全部合法 | 12/12入库；1次模型调用 |
| Retry rate | 未设单独阈值 | 0次额外调用/1次调用 |

本次输入Token总量1241（含缓存用量）、输出1282，调用耗时13472ms，端到端13.55s，unknown usage调用0。类型错例F003、F005、F007、F009及预测值见 `work/phase1-fix-20261012/real-evaluation.json`。模型输出非确定性：同日另一轮为9/12；不得挑选较高一次宣称达标。参考标签未经人工复核，不能将66.67%当作正式业务准确率，也不能把90%目标写成已完成。

## 尚需关闭的 Gate

1. **质量与标签：** 三文鱼或指定业务评审者复核12条参考标签及争议样本，确定 `feedback_type` 与 Intent 的判分口径；按认可数据重新评测。若正式指标低于目标，再针对错误类型修改版本化Prompt或模型策略，并重新测试。当前不擅自改标签来提高分数。
2. **人工 `/docs`：** 本轮真实HTTP链路通过，浏览器中的文件选择上传仍待三文鱼亲自操作。需在隔离服务中完成导入、分类、分页、Raw对照及Retry。
3. **部署数据库：** 本轮使用独立PostgreSQL16.2；用户现有数据库及PostgreSQL17部署未做破坏性迁移验证。上线前备份并核对0005旧Run快照与结果行。

上述是条件通过的边界。三文鱼尚未批准 Phase 1，**不得启动 Embedding、pgvector、Clustering 或其他 Phase 2 功能**。

## 三文鱼复测步骤

在 `backend` 目录执行以下隔离命令，先看自动回归与旧数据迁移；脚本自行创建并清理临时PostgreSQL，不要把自己的业务库传给 `TEST_DATABASE_URL`：

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
uv sync --locked
uv run ruff check .
uv run pytest -q
uv run --with pgserver==0.1.4 python scripts/verify_phase1_acceptance.py
uv run --with pgserver==0.1.4 python scripts/phase1_recheck_migration.py
```

预期SQLite92通过/3跳过，PostgreSQL95通过，Alembic current 0005、迁移脚本 `passed: true`。如数据或代码改变，应以新执行结果为准。

真实接口另开终端启动 `uv run --with pgserver==0.1.4 python scripts/phase1_acceptance_server.py`，打开 `http://127.0.0.1:18081/docs`：创建项目、上传 `data/demo_feedback.csv` 两次、保存 Raw、POST analysis、GET Run、按offset=0/5/10分页GET results，再比较 Raw。预期12条、重复导入跳过12、Raw不变；Run的Token须包含供应商缓存用量。故障测试可在隔离服务上运行 `uv run python scripts/phase1_acceptance_http.py`，按新报告的 `manual_retry_fixture` 取**本次新Run ID**验证Retry。完成后 `touch ../work/phase1-acceptance/stop-server` 清理。

真实评测运行 `uv run --with pgserver==0.1.4 python scripts/run_classification_eval.py`，检查新生成 `work/eval-results/*.json` 的时间、哈希、Target/Actual、失败退出码。人工复核标签后记录阶段决定。

**STOP，等待三文鱼人工复测及明确批准。**

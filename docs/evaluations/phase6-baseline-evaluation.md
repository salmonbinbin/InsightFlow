# Phase 6 Baseline Evaluation

日期：2026-10-14。评测负责人：Codex Supervisor。**结论：PARTIAL**。本报告的数值来自隔离 PostgreSQL 上的真实 MiniMax-M3 运行；12 条样本及参考标签未经人工复核，不构成正式业务准确率。

## 数据与配置

使用 `data/demo_feedback.csv` + `data/golden_dataset.csv`，12 条，`demo` 来源、`full` split、内容哈希 `8e00033143dd1937b5da83c44675a82fb30939d5de79b6b8ec28a40c2676310e`。当前规模不足，未强行拆分 Development/Validation/Test；固定 Test Set 和人工 Ground Truth 尚未建立。运行时数据库为一次性隔离 PostgreSQL；原始机器可核对证据保存在 Git 忽略目录 `work/phase6-supervision/`。

成功轮：分类 MiniMax-M3、`feedback-classification-v1`、计算版本 `phase6-metrics-v1`；聚类 `local-hash-v1`、average linkage、关闭 LLM 复核。Insight 轮使用 MiniMax-M3、`insight-generation-v2`，DB 快照来源标为 `unverified`，不推断它是生产数据。`local-hash-v1` 是本地哈希向量，不代表真实语义 Embedding。

## Target / Actual

下列 Target 是长期总纲中的产品目标或硬 Guardrail 方向，不能当作本轮通过门槛。`NOT MEASURED` 表示没有合格的分母或人工判定。

| 指标 | Target | Actual（成功分类轮） | 依据与限制 |
|---|---:|---:|---|
| Feedback Type Accuracy | ≥90% | 8/12 = 66.67% | 未审核参考标签 |
| Macro F1 | 尚未设定 | 0.617347 | 12 条、类别不均衡 |
| Intent Accuracy | ≥85% | NOT MEASURED | Intent 未形成受控标签 |
| Severity Exact / MAE | 尚未设定 | 8/12 = 66.67% / 0.333333 | Severity 1—5 |
| 分类源 Pipeline Success | 尚未设定 | 12/12 = 100% | 表示完成调用与持久化，不表示标签正确 |
| Evidence Existence | 接近 100% | 12/12 = 100% | 独立 Insight 轮；仅验证 DB 引用与来源快照 |
| Evidence Validity | 尚未设定 | NOT MEASURED | 需要人工判断引用是否支持结论 |
| Groundedness | 尚未设定 | NOT MEASURED | 需要逐条核对事实 |
| Hallucination Rate | 越低越好 | NOT MEASURED | 尚无人工裁决 |
| Requirement / PRD 语义质量 | 尚未设定 | NOT MEASURED | Phase 5 真实 AI 草稿未入库 |

成功分类轮模型调用累计 48,810 ms，墙钟 48.893 秒，输入/输出 1,241/1,294 tokens；Cost `NOT MEASURED`，没有可靠单价。评测 Run 的 `partial_failed` 是标签差异造成的结果状态；源分类 Run 为 `completed`，Pipeline Success 仍为 12/12。

## 阈值实验

以下是成功分类轮上的 `local-hash-v1` 探索性结果。微平均 purity 不能单独作为阈值选择依据：阈值 0.30 几乎全拆成单样本簇，purity 高但 fragmentation 更高；阈值 0.60 过度合并。没有使用正式 Test Set 调参。

| 距离阈值 | 簇数 | Purity micro | Coverage | Fragmentation mean | Over-merge ratio |
|---:|---:|---:|---:|---:|---:|
| 0.30 | 11 | 1.000000 | 1.000000 | 1.222222 | 0.000000 |
| 0.45 | 8 | 0.916667 | 1.000000 | 1.000000 | 0.125000 |
| 0.60 | 4 | 0.583333 | 1.000000 | 1.000000 | 0.500000 |

## Insight 与失败轮

修正来源标记后的真实 Insight 轮：分类源 12/12 completed；Insight 源生成 4 条，2 success、2 needs_validation、0 failed；模型输入/输出 6,886/1,597 tokens，累计延迟 30,817 ms。Insight Evidence 的 12/12 个 DB 引用存在，`dataset_provenance=unverified`，内容哈希由引用与反馈正文哈希形成。语义三指标保留 NULL。该轮与上表成功分类轮**是不同运行**，不能把其时延或样本数相加为同一次端到端指标。

另一次真实分类轮经历 3 次尝试、47,721 ms，源 Run 0/12 success、12/12 failed，tokens 未知。评测中类型准确率 0 是因为没有预测，不能解释为模型语义准确率 0；该轮 Pipeline Success 为 0，失败阶段为 classification。保留此轮以追踪模型协议/调用可靠性。

## 失败样本与模式

成功分类轮的差异样本为 F002（严重度 4→3）、F003（UX→feature_request，3→2）、F004（5→4）、F007（content_issue→bug）、F008（feature_request→bug）、F009（content_issue→bug，4→3）。可见严重度偏低和内容问题被归为 bug 的倾向。此处为未经审核参考标签的差异，不是最终错误裁决。评测表保存 case ID、预期与实际的哈希，默认不存反馈明文；需要在受控本地数据集核对详情。

## 人工复核 Rubric

1. Evidence Existence：自动核对 Evidence ID、反馈 ID、项目归属及来源快照。引用存在不等于支持 Insight。
2. Evidence Validity：人工逐条给每个引用标记“支持 / 不支持 / 不确定”，写出支持或冲突的原文片段及审阅人、时间；不确定先裁决，不进入分母。有效率为支持引用数 / 已裁决引用数。
3. Groundedness：把每条 Insight 的核心事实拆成可核证断言，逐一对照引用；全被支持才将该 Insight 判为 grounded。比率为 grounded Insight 数 / 已完成逐条复核 Insight 数。
4. Hallucination：事实性断言没有 Evidence 支持即标记，包括无依据的流失率、收入变化或用户比例。**Hallucination Rate = 含至少一个无支持事实断言的 Insight 数 / 已完成逐条复核的 Insight 数**。不确定项待裁决；未审核时显示 `NOT MEASURED`，不能写 0%。
5. Requirement/PRD：人工审查 Problem、Target User、User Story、Solution、Priority、Success Metrics、Risks，核对 PM Final Priority 与 Requirement；逐条比对 PRD 核心需求、可测试验收标准、失败/重试/边界、MVP In/Out Scope 和所有商业数字的 Evidence。没有合格样本时均为 `NOT MEASURED`。

本轮没有启用 LLM Judge，因此 Judge Prompt/模型/结构化输出与超时测试不适用。未来若引入，需固定 Rubric、记录版本/理由，并避免生成者自评或加人工抽样。

## 回归与改进建议

`EvaluationRun` 保存数据集名称、版本、哈希、来源、模型、Prompt、Pipeline/计算版本；`evaluation_metrics` 与逐例 `evaluation_cases` 支持同键指标比较及缺失指标提示。比较时须固定同一数据集哈希与人工标签状态。下轮先建人工审核的 Validation/Test 集，再测真实语义 Embedding、完成 Phase 3 人工语义裁决；Phase 4 评分公式/边界及 PM override 由既有 Phase 4 测试保护，但 Phase 4/5 **尚无专门落库的 Phase 6 Artifact Evaluation**。本阶段不为提高分数改 Phase 1—5 Prompt 或业务逻辑。

## 复现

在 `backend` 下执行 `uv run python scripts/run_evaluation.py --help` 与 `uv run python scripts/run_insight_evaluation.py --help`；提供项目、已完成的源 Run ID 和固定数据集后运行。API 为 `POST /evaluations`、`GET /evaluations/{id}`、`GET /evaluations/{id}/metrics`、`GET /evaluations/{id}/cases`。真实模型脚本及 JSON 保留在本机忽略目录，依赖本机私有配置，不进 Git。

# Phase 6 Delivery Report — Evaluation System

日期：2026-10-14（Asia/Shanghai）。三文鱼授权本阶段的 Codex Supervisor → Claude Code Worker 协作、独立验收和本地提交。

## Status

**PARTIAL。** 分类、聚类、Insight Evidence 的评测数据层、计算、逐例追踪、API/CLI、版本比较与真实 Baseline 已实现并通过工程回归。正式人工标签、真实语义 Embedding、Insight 的 Evidence Validity/Groundedness/Hallucination、Phase 4/5 Artifact Evaluation 和完整 Feedback→PRD Pipeline 均未测得；这些限制不能由工程测试或 12 条演示样本代替。交付后 **STOP**，等待三文鱼人工验收，不进入 Phase 7。

## Evaluation Architecture

`Golden/Demo Dataset → 已完成的来源 Run → 预测快照 → 确定性指标 + 逐例差异 → Evaluation Run/Metric/Case → 版本比较`。来源 Run 与 Raw Feedback 只读；评测表独立追加。默认保存输入/预期/实际的哈希，不存反馈明文；`store_raw` 需显式选择。Insight DB 快照为 `unverified`，内容哈希覆盖 Evidence 引用与反馈正文哈希。`evaluation_runs` 记录数据集、来源、模型、Prompt、Pipeline/计算版本、状态、时间、计数、tokens、延迟与可选成本；`evaluation_metrics` 记录数值、样本数、类型；`evaluation_cases` 保留每例结果和失败阶段。

Phase 1 指标：Feedback Type Accuracy、Macro F1、Severity Exact/MAE；Intent 只有受控词表时才算 Exact Match。Phase 2：Purity、Coverage、Fragmentation、Over-merge、簇数与阈值比较。Phase 3：Evidence ID 存在性自动核对；语义三指标存 `human_reviewed`、NULL、`NOT MEASURED`。Pipeline Success/Failure Stage 基于源 Run 结果，分类标签错误不计成流程故障。Latency 与 Tokens 取来源 Run，未知值为 NULL；Cost 无可靠价格则 NULL。Regression 比较固定数据集/版本的同键指标，缺失指标显式报告。

## Dataset Used / Actual Baseline

`demo_feedback.csv + golden_dataset.csv`，12 条，demo/synthetic/unreviewed reference，完整集未拆分。成功分类轮 MiniMax-M3 / `feedback-classification-v1`：源 Run 12/12 completed；类型 8/12 = **66.67%**，Macro F1 **0.617347**，Severity Exact 8/12 = **66.67%**、MAE **0.333333**；Intent `NOT MEASURED`。分类 Pipeline Success **100%**，输入/输出 **1,241/1,294 tokens**，模型累计延迟 **48,810 ms**，墙钟 48.893 秒；Cost `NOT MEASURED`。长期总纲的类型准确率目标 ≥90%、Intent 目标 ≥85% 是 **Target**，不是本轮 Actual 或已通过结论。

`local-hash-v1` 阈值 0.30/0.45/0.60 分别产生 11/8/4 簇，Purity micro 1.0/0.916667/0.583333，Coverage 均 1.0，Fragmentation 1.222222/1.0/1.0，Over-merge 0/0.125/0.5；这是哈希向量探索结果。修正来源后的独立真实 Insight 轮生成 4 条（2 success、2 needs_validation），12/12 Evidence DB 引用存在，6,886/1,597 tokens、30,817 ms；语义三指标 `NOT MEASURED`。另一失败分类轮 0/12 completed、三次尝试、47,721 ms，tokens 未知，证明调用/协议可靠性风险，不能解释为语义准确率 0。

失败样本 F002/F003/F004/F007/F008/F009，集中在严重度偏低、UX/功能请求混淆、内容问题归为 bug。标签未经人工复核，仍待裁决。完整结果、人工 Rubric、目标/实际区分与限制见 [Baseline Evaluation](evaluations/phase6-baseline-evaluation.md)。

## Files Changed / Database Migrations

新增 `backend/alembic/versions/0011_phase6_evaluation.py`、`backend/app/models/evaluation.py`、`backend/app/schemas/evaluation.py`、`backend/app/api/evaluations.py`、`backend/app/evals/{dataset,metrics,regression,runner,insight_runner}.py`、`backend/scripts/run_evaluation.py`、`backend/scripts/run_insight_evaluation.py`、7 个 `backend/tests/test_phase6_*.py` 与本报告/Baseline 文档；更新模型/API/evals 的注册入口以及项目状态记录。0011 仅增加 3 张评测表和约束；0001—0010、Phase 1—5 Prompt/业务实现及既有测试没有改动。隔离 PostgreSQL base→head→base→head 与 Alembic check 已通过。回退会删除评测表和数据，仅在隔离环境验证，不应在现有数据上直接执行。

## APIs / CLI

`POST /evaluations` 启动已有分类/聚类 Run 的评测，`GET /evaluations/{id}`、`GET /evaluations/{id}/metrics`、`GET /evaluations/{id}/cases` 读取结果。分类/聚类 CLI 为 `scripts/run_evaluation.py`；Insight CLI 为 `scripts/run_insight_evaluation.py`。运行参数见各脚本 `--help`。没有引入 LLM Judge，相关版本化输出/无效输出/超时测试在本实现中不适用。

## Tests Added and Results

Claude Worker 新增数据集、公式、回归比较、Runner、Insight Runner、API、CLI 7 组专项测试。Codex 于最终写入停止后独立执行：SQLite **582 passed, 5 skipped**；隔离 PostgreSQL **587 passed** 且 `POSTGRES_VERIFICATION_PASSED`；`uv run ruff check app tests alembic scripts` 通过。PostgreSQL 验证包含迁移升级/回退/再次升级和 Alembic check。真实模型脚本在隔离 PostgreSQL 运行；本机原始 JSON 留在 Git 忽略目录，不提交密钥、临时数据库或运行数据。

## Supervisor → Worker / 未交付项

Codex 负责范围、数据/验收、文档与 Git；Claude Code 只修改白名单实现与新增测试，禁止 Git/Bash/push。已保存 jobId：`c04aca2c45084821ae962850dd23078a`、`3475d8fb9c9c46f5bd03ae806aced69f`、`28873915062644ecbea620a5ab59d8d3`、`37946e87e6b34e17afe858718be45a9e`、`240a94615a4e4df19965d54e47f902d6`、`d5a7f2f936454da3833c24b2ad79621d`、`fc3ee37485a1498fb4b3e7ec6d7da9fd`、`797c2a92f3f847bb8cff2bb263ea33da`、`a8f7739ca243452d808a002e1699c684`。Phase 4 Artifact 任务 `04fe3f4a2ec5457e84624d4c0e904422` 及同一 Claude 会话 `35abddf9-e118-49cc-ba14-23556c7cbf0e` 的收窄任务 `3f7bdc4651504c748d7c84f4554c3486` 都在 600 秒超时，目标文件未写入，故 **Phase 4/5 专项 Artifact Evaluation 未交付**。现有 Phase 4 评分/优先级/PM override 测试仍参与全量回归。

## Known Evaluation Limitations / Next Recommended Phase

本次分类分数仅是对未经复核参考标签的差异率；模型非确定性，另一次成功轮数值不同。固定正式 Test Set、人工标签和审阅者一致性尚缺；Phase 2 真实语义 Embedding、Phase 3 人工支持性/幻觉判定、Phase 4 正式业务评分质量及 Phase 5 真实 AI PRD 入库限制保持原状态。没有得到端到端 Feedback→PRD 的统一 Pipeline Success、总时延或成本，均为 `NOT MEASURED`。下一步建议先人工审核标签和 Insight/PRD 样本，再扩展 Phase 4/5 Artifact Evaluation；这不是自动进入 Phase 7 的授权。

## Git Commit

实现与本报告由 Codex 本地提交；精确提交号以 `git log -1 --oneline` 核对，不 push。

## User Retest Steps

1. 在 `backend` 执行 `uv sync --locked`、`uv run pytest -q`、`uv run ruff check app tests alembic scripts`、`uv run --with pgserver==0.1.4 python scripts/verify_postgres.py`。
2. 在隔离数据库升级 0011，导入同一固定数据集并生成已完成分类/聚类 Run；运行 `uv run python scripts/run_evaluation.py --help` 查看参数，执行评测并用 API 读取 metrics/cases，核对数据集哈希、模型/Prompt、失败 case 与 NULL 指标。
3. 对有 Evidence 的 Insight Run 执行 `uv run python scripts/run_insight_evaluation.py --help`，核对引用存在性和三个 `NOT MEASURED`；按 Baseline Rubric 人工判定，不将 12/12 引用存在率解释为 Groundedness。

**STOP：等待三文鱼人工验收 Phase 6。**

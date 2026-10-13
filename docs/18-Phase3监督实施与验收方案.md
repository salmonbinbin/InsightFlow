# Phase 3 监督实施与验收方案

日期：2026-10-12。负责人：三文鱼；Supervisor：Codex；Worker：Claude Code。

## 授权与基线

用户本次明确授权Phase3、由Codex确定架构及验收并指挥Claude实现，验收后本地commit，不push或推进下一阶段。项目原来没有AGENTS.md和.git；Codex建立入口与本地版本基线以实现本次监督要求。基线commit b735522。原始CSV CRLF及Markdown双空格保留，基线空白检查已记录；未重写原文件。

本轮开发前SQLite 161 passed/5 skipped；Ruff通过；隔离PG16.2 166 passed，upgrade/check/downgrade/reupgrade/check通过，head=0007。Phase2独立报告仍FAIL：真实语义Embedding未配置、真实LLM质量有波动。Phase3不能宣称完整语义端到端已验收。

## 架构与范围

采用现有FastAPI/SQLAlchemy/同步调用模式，逐簇生成并独立提交。与单次无状态生成相比，持久化run支持失败重试和审计；与队列方案相比，无新增基础设施。

新增insight_runs（专用运行/来源快照/模型Prompt/五状态/计量）、insights、insight_evidence；允许新增insight_run_items记录逐簇状态/失败。独立run避免侵入Phase1/2共用AnalysisRun。0008新增迁移，不改历史迁移。

输入显式指定本项目已completed的clustering_run_id；验证其classification_run_id快照、成员所属项目/分类run和原始feedback一致。排除outlier和空审计父簇，不从最新run猜来源。保存原始簇ID及成员快照，重试不得扩大输入。没有可用簇返回409。仅有1—2条证据可生成needs_validation，绝不包装为已验证洞察；所有结果仍需人工审阅，3条及以上仅是draft。

每簇先确定性选最多8条证据，优先代表性（similarity）、最高severity和不同表达；原文由数据库读取，模型不得返回/代写quote。结构输出title/pain_point/affected_segment/insight/recommendation/confidence及evidence条目（feedback UUID、relevance_score、evidence_type=representative/high_severity/diverse_expression）。每条证据必须属于输入候选、同项目、同分类来源、该簇，唯一且非空；正常簇至少3条，少量簇使用全部。未知人群写未知，商业效果不得无证据推断。Prompt约束反馈是数据；硬校验ID不是语义正确性证明。

唯一键(run_id,cluster_id)、(insight_id,feedback_id)，数值严格finite/非bool/[0,1]、字符串长度与必填校验、外键/CHECK约束。Insight和Evidence同事务成功或回滚，原文和历史不修改。模型/Prompt绑定；失败重试只处理未成功项、completed retry无副作用；PG跨进程锁/SQLite本地锁，异常安全摘要，按实际调用累积含失败的tokens/latency/attempts，未知usage另计。

API：POST /projects/{project_id}/insights/generate（body clustering_run_id）；GET /insight-runs/{run_id}；POST /insight-runs/{run_id}/retry；GET /projects/{project_id}/insights（可选run_id+分页）；GET /insights/{insight_id}；GET /insights/{insight_id}/evidence。同步200，不伪称202异步。读接口包含来源追溯；证据返回数据库原文。

## Worker修改白名单

仅新增/实现 backend/app/models/insight.py、app/schemas/insight.py、app/services/insight_service.py、app/services/insight_runner.py、app/services/insight_client.py、app/agents/prompts/insight_generation_v1.py、app/api/insights.py；允许最小修改app/models/__init__.py、app/main.py；新增alembic/versions/0008_phase3_insight_evidence.py、tests/test_phase3.py、scripts/verify_phase3_migration.py、scripts/verify_phase3_http.py。若必要可增加同前缀Phase3专用模块，但须先向Supervisor报告。

配置复用现有llm_*，Phase3 retries固定2且记录；不改.env、依赖、旧测试/迁移/Prompt、Phase2代码或其他业务。文档与最终验收报告由Codex更新。

允许命令（cwd backend）：uv run pytest tests/test_phase3.py -q；uv run pytest -q；uv run ruff check app tests alembic scripts；uv run ruff format 白名单文件；uv run --with pgserver==0.1.4 python scripts/verify_phase3_migration.py。禁止Git、安装依赖、真实模型调用、联网下载、服务部署和操作现有库。MCP allowedTools仅开放Read/Grep/Glob/Edit/Write与上述测试命令；wrapper强制Git禁止。限制是工具/协作边界，不冒充OS沙盒。

## 验收标准

1. 原测试完整回归，新增成功、非法Schema/ID/跨项目/跨run、无证据、重复证据、空簇/outlier/小簇、未知人群、分页及404/409/422。
2. 重试只补失败、历史独立、模型/Prompt/来源漂移拒绝、completed no-op、并发锁、中断恢复、DB写入失败回滚及调用计量、供应商错误不得泄漏哨兵。
3. Raw全字段前后相同，Phase1/2旧测试/迁移/Prompt无diff。
4. 隔离PG迁移含带数据0007→0008→0007→0008、Raw与Phase2数据保留、模型迁移一致、真实FK/唯一/CHECK；全量PG回归。
5. Ruff、改动文件格式、后端编译与HTTP/OpenAPI验证；前端未实现，无前端构建可运行。
6. 真实Phase3模型评测由Codex独立运行，至少10簇的受控合成输入可作为隔离Phase3评测，明确不是完成Phase2真实Embedding验收；保存失败与成功样本、模型/Prompt/usage/latency，程序Evidence有效率与语义Groundedness/幻觉分开统计。无人工标签不得编造正式语义准确率。
7. Codex独立diff和测试，失败续接同一Claude会话，保存所有jobId/threadId及证据。验收报告、工作记录、README/交接和决策记录同步，之后Codex本地提交；用户人工复核至少10洞察待办，STOP。

## 2026-10-13 监督补充

Claude首两批各触发600秒工具上限，续接同一会话完成核心返工。独立验收脚本改由Codex编写 `backend/scripts/verify_phase3_supervisor.py`，并新增保护测试 `backend/tests/test_phase3_supervisor.py`；Worker禁止修改这两个文件。首轮真实模型见work下20261012T174433Z，已执行v1，因此后续Prompt修订使用新增 `insight_generation_v2.py`，允许Worker增加该文件并修改Phase3 client/runner导入；保留v1历史。真实评测初见结构有效但存在过推断，不能将程序ID有效率当语义正确率。

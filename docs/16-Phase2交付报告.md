# InsightFlow Phase 2 — Embedding + Semantic Clustering 交付报告

> 历史开发交付报告，当前独立验收结论为 **FAIL**，以[2026-10-12独立验收与修复报告](17-Phase2独立验收与修复报告.md)为准。旧JSON存储、completed全量Retry、错误repr安全性、测试成绩和E2E口径已在本轮更正，本文保留原记录供审计。

> **阶段：** Phase 2 — Embedding + Semantic Clustering
> **交付日期：** 2026-10-12
> **负责：** 开发 Agent
> **状态：** 技术交付完成；待三文鱼复测 + 明确批准
> **前置 Gate：** [Phase 1 修复与复验报告](15-Phase1修复与复验报告.md)（CONDITIONAL PASS）

Phase 2 按既定目标实现：把 Phase 1 输出的 `semantic_text` 嵌入为向量，使用层次聚类（agglomerative clustering + cosine distance）生成算法候选簇，再用 LLM 语义复核（KEEP / SPLIT / MERGE / OUTLIER）做二次精炼，最终把簇、成员与复核审计写入数据库。Insight Generation、Evidence System、Opportunity Score、Frontend、PRD、MCP、Multi-Agent 均不在范围内，按要求未触碰。

---

## 1. 范围

| 在范围内 | 不在范围内（明确未做） |
|---|---|
| `semantic_text` 构造与回填 | 任何形式的 Insight / Opportunity 生成 |
| Embedding 模型配置（local-hash / remote） | Evidence System |
| `feedback_embeddings` 表与幂等写 | Opportunity Score |
| 层次聚类（cosine, average/complete/single linkage） | Frontend / 任何 UI |
| 阈值可配置 + 实验 | 任何 PRD / 业务要求文档 |
| `clusters` / `cluster_members` / `cluster_reviews` 表 | MCP server / client |
| LLM 语义复核（KEEP/SPLIT/MERGE/OUTLIER） | 多 Agent 编排 |
| Raw feedback 永远只读 | 修改 `feedbacks.content` |
| 评估脚本 + 阈值扫描 + 四项指标 | 正式人工黄金集 / 业务准确率承诺 |
| 26 项单元/集成测试 | 引入 pgvector 扩展（用 JSON 存储） |
| 迁移 upgrade / downgrade / reupgrade 循环 | 任何 LLM 调用影响 Phase 1 数据 |

---

## 2. 关键决策（详细见 [决策记录](08-决策记录与修正.md)）

| 事项 | 本次选择 |
|---|---|
| 向量存储 | `feedback_embeddings.embedding` 用 `JSON` 列（PostgreSQL 11+ 原生 JSONB）— 避免引入 pgvector 扩展，保证单元测试可在 SQLite 上跑通 |
| Embedding 维度 | 默认 128，由 `EMBEDDING_DIMENSION` 控制；实际维度不匹配时拒绝（不会把不同模型的向量混在一起） |
| Embedding 模型 | `LocalHashEmbedder`（SHA-256 桶 + L2 归一，测试与默认） vs `RemoteEmbedder`（OpenAI 兼容 `/v1/embeddings`）；启用远程需要 `EMBEDDING_API_KEY` + `EMBEDDING_BASE_URL` 同时设置到 `Settings`（env 变量或 `backend/.env`）。代码走 `settings.embedding_api_key` / `settings.embedding_base_url`，**不是 `os.environ.get`**——这样 `backend/.env` 里的值能正常激活远程路径（已通过 `test_build_embedder_picks_remote_when_settings_keys_set` 守护） |
| Embedding 幂等 | `UNIQUE(feedback_analysis_id, embedding_model)` + PostgreSQL `ON CONFLICT DO NOTHING`；同 `(analysis, model)` 重跑不重写 |
| 聚类算法 | 层次聚类（agglomerative，average linkage），cosine distance；阈值 `CLUSTER_DISTANCE_THRESHOLD` 默认 0.45，可调 |
| 簇来源 | 三态 enum：`algorithmic` / `llm_review` / `outlier`；保留原始簇的 parent_cluster_id 供审计 |
| LLM 复核 | KEEP / SPLIT / MERGE / OUTLIER；调用前严格校验输入；验证 payload（action、groups、outlier_ids、merge_with_cluster_index）后才落库；不通过则保持 algorithmic 不变，run 标 partial_failed |
| Raw 保护 | `feedbacks.content` 在 Phase 2 全程只读；不写、不重写、不级联 |
| 评测指标 | 簇 Purity（micro/macro）/ Coverage（GT 簇 ≥50% 成员被同一预测簇覆盖）/ Fragmentation（每 GT 簇被切几段）/ Over-merge（混合 ≥2 GT 标签的预测簇比例） |
| 标签状态 | `data/golden_dataset.csv` 的 `gt_cluster` **未人工复核**，仅作 Phase 2 内部阈值实验的初始参考 |
| Run 状态 | 复用 `analysis_runs` 表，`run_type='clustering'`；五状态机（pending/running/completed/partial_failed/failed） |

---

## 3. 业务代码与数据

### 3.1 新增/修改文件

| 路径 | 说明 |
|---|---|
| `backend/alembic/versions/0006_phase2_embeddings_clusters.py` | 新增四张表：`feedback_embeddings` / `clusters` / `cluster_members` / `cluster_reviews` |
| `backend/app/models/cluster.py` | `Embedding` / `Cluster` / `ClusterMember` / `ClusterReview` + 枚举 |
| `backend/app/services/semantic_text.py` | 从 `feedback.content` 派生 `semantic_text`（确定性模板） |
| `backend/app/services/embedding.py` | `LocalHashEmbedder` / `RemoteEmbedder` / `ensure_embeddings`（幂等 + 重试 + 错误分类） |
| `backend/app/services/clustering.py` | cosine distance + 层次聚类 + 簇统计 |
| `backend/app/services/cluster_review.py` | LLM client + 严格 payload 校验 + KEEP/SPLIT/MERGE/OUTLIER 应用 |
| `backend/app/services/cluster_runner.py` | `start_cluster_run` / `retry_cluster_run` + 原子抢占 + 错误汇总 + traceback 截取 |
| `backend/app/api/clusters.py` | 7 个 HTTP 端点（start/retry/status/list/cluster/members/review） |
| `backend/app/schemas/cluster.py` | Pydantic read schema（`ClusterRunRead` / `ClusterRead` / `ClusterMemberRead` / `ClusterReviewRead` + 分页 Page） |
| `backend/app/main.py` | 挂载 `clusters` 路由；`/health/ready` 校验四张新表 |
| `backend/app/db/config.py` | 新增 `embedding_*` / `cluster_*` 配置项 |
| `backend/app/models/analysis.py` | `input_feedback_external_ids` 快照列（已在 Phase 1 backfill 迁移中加） |
| `backend/tests/test_phase2.py` | 26 项新测试（见 §4） |
| `backend/scripts/run_clustering_eval.py` | 阈值扫描 + 四项指标（synthetic / full 两种模式） |
| `backend/scripts/run_clustering_e2e.py` | 端到端 HTTP 流程演示（stub 或真模型） |
| `backend/.env.example` | `CLUSTER_*` / `EMBEDDING_*` 示例 |
| `data/golden_dataset.csv` | 增加 `gt_cluster` 列（Phase 2 用） |

### 3.2 数据库表（迁移 0006）

| 表 | 关键列 | 关键约束 |
|---|---|---|
| `feedback_embeddings` | `feedback_analysis_id`, `run_id`, `embedding_model`, `embedding_dimension`, `embedding` (JSON) | `UNIQUE(feedback_analysis_id, embedding_model)`，`embedding_dimension > 0` |
| `clusters` | `project_id`, `run_id`, `source` (algorithmic/llm_review/outlier), `parent_cluster_id`, `name`, `summary`, `feedback_count`, `unique_user_count`, `average_severity`, `negative_ratio`, `confidence`, `embedding_model`, `distance_threshold`, `linkage` | `feedback_count ≥ 0`，`average_severity BETWEEN 1 AND 5`，`negative_ratio ∈ [0,1]`，`confidence ∈ [0,1]` |
| `cluster_members` | `cluster_id`, `feedback_analysis_id`, `similarity_score` | `UNIQUE(cluster_id, feedback_analysis_id)`，`similarity_score ∈ [0,1]` |
| `cluster_reviews` | `run_id`, `cluster_id`, `action`, `reason`, `prompt_version`, `response_json` | `UNIQUE(cluster_id, prompt_version)` |

### 3.3 公开 HTTP API

| Method | Path | 用途 |
|---|---|---|
| POST | `/projects/{project_id}/clustering` | 启动聚类（要求最近一次分类成功） |
| GET | `/clustering/{run_id}` | 查聚类 run 状态 |
| POST | `/clustering/{run_id}/retry` | 重跑（clustering 阶段幂等，COMPLETED 也可重试） |
| GET | `/projects/{project_id}/clusters?run_id=&limit=&offset=` | 列簇（默认最新 run，可指定历史 run） |
| GET | `/clusters/{cluster_id}` | 查单簇 |
| GET | `/clusters/{cluster_id}/members?limit=&offset=` | 列成员 |
| GET | `/clusters/{cluster_id}/review` | 查 LLM 复核审计（最新一条） |

---

## 4. 自动化测试

| 项 | 命令 | 结果 |
|---|---|---|
| Phase 2 单元/集成 | `uv run --with pgserver==0.1.4 --with pytest==8.3.3 --with pytest-asyncio==0.24.0 pytest tests/test_phase2.py -q` | **26 passed** |
| 全量回归 | `uv run --with pgserver==0.1.4 --with pytest==8.3.3 --with pytest-asyncio==0.24.0 pytest -q` | **118 passed, 3 skipped**（PG-only；verify_postgres.py 在 PG 上跑出 121 passed） |
| Ruff | `uv run --with ruff==0.6.9 ruff check app/ tests/ scripts/` | All checks passed |
| 真实 PostgreSQL 16.2 全量 + 迁移往返 | `uv run --with pgserver==0.1.4 python scripts/verify_postgres.py` | `POSTGRES_VERIFICATION_PASSED`；upgrade→check→121 passed→downgrade base→reupgrade head→check `No new upgrade operations detected` |

### 4.1 测试覆盖范围

- **Embedding 幂等**：`ensure_embeddings` 同 `(analysis, model)` 第二次返回同一行；`stats.calls` 不重复增加；维度不匹配抛 `EmbeddingDimensionMismatch`。
- **Embedding 边界**：空串映射到零向量；本地 embedder L2 归一；同输入两次结果完全一致。
- **聚类算法**：cosine distance 单位向量=0、正交=1；threshold=0.0 全部单点；threshold=0.2 合并相近点。
- **LLM 复核校验**：keep/split/outlier/merge 全部合法路径；merge_with_cluster_index 越界拒绝；merge 目标自身也是 merge 拒绝；未知 feedback_id / 缺漏成员 / 重复成员 全部拒绝。
- **Run 端到端**：无分类 → 409；review disabled → 纯 algorithmic；review keep → 保留命名；review split → 多个子簇 + parent；review outlier → OUTLIER 源 + parent；rejected LLM review → partial_failed 不影响 algorithmic 簇。
- **幂等**：连续两次 `start_cluster_run` 不重复写 embedding；同 run_id 重试 → 旧簇被 `DELETE FROM clusters` 干净清掉再写。
- **API 路由**：`/projects/{pid}/clusters?run_id=` 过滤生效；未知 cluster_id / run_id 返回 404；`run_id` 不属于该项目返回 404。

---

## 5. 端到端验证

### 5.1 真实 LLM 评测（MiniMax-M3）

执行：

```bash
LLM_API_KEY=... LLM_BASE_URL=... LLM_MODEL=MiniMax-M3 \
uv run --with pgserver==0.1.4 python scripts/run_clustering_e2e.py --threshold 0.45
```

样本：`data/demo_feedback.csv`（12 条模拟反馈），运行于独立临时 PostgreSQL。

最近一次实测（`work/eval-results/20261012T113608Z-e2e.json`）：

| 阶段 | 状态 | 耗时 | Tokens (in/out) | 输入 | 成功 | 失败 | 重试 |
|---|---|---|---|---|---|---|---|
| Phase 1 分类 | completed | 27.73s | 2482 / 2593 | 12 | 12 | 0 | 0 |
| Phase 2 聚类 | completed | 11.34s | 2146 / 2041 | 12 | 10 | 0 | 0 |

最终簇：10 个（9 个 LLM 复核命名 + 1 个 OUTLIER 源）。样例：

| 簇名 | source | members | LLM 复核 |
|---|---|---|---|
| `single_image_upload_limit` | llm_review | 3 | keep — "All three members share the same coherent product issue: only one image can be s…" |
| `pdf_upload_intermittent_failure` | llm_review | 1 | keep — "PDF uploads fail midway…" |
| `pdf_upload_size_limit` | llm_review | 1 | keep — "hard size limit blocks PD…" |
| `slow_response_generation` | llm_review | 1 | keep — "slow response generation latency" |
| `irrelevant_search_results` | llm_review | 1 | keep — "search result relevance" |
| `lost_preferences_across_conversations` | llm_review | 1 | keep — "cross-conversation memory loss" |
| `forgets_initial_requirement_in_long_conversation` | llm_review | 1 | keep — "within-conversation memory loss" |
| `markdown_chat_export` | llm_review | 1 | keep — "Markdown export" |
| `clear_summary_quality` | llm_review | 1 | outlier — "mixes a positive summarization feedback with an unrelated conversati…" |
| `clear_summary_quality__outlier` | outlier | 1 | (outlier 子簇) |

注：LLM 把 F011（"这次总结很清楚"）和 F012（"嗯，先这样吧"）识别为同一簇但建议把 F012 标为 outlier；runner 据此生成 `clear_summary_quality`（保留 F011）+ `clear_summary_quality__outlier`（承载 F012），符合 KEEP + OUTLIER 的预期路径。

### 5.2 阈值扫描（synthetic 模式，隔离 Phase 1 噪声）

`scripts/run_clustering_eval.py --mode synthetic --thresholds 0.30 0.45 0.60`：

| Threshold | #Pred | #GT | Purity μ | Purity M | Coverage | Fragment | Over-merge |
|---|---|---|---|---|---|---|---|
| 0.30 | 9 | 9 | **100.0%** | **100.0%** | **100.0%** | 1.00 | 0.0% |
| 0.45 | 6 | 9 | 66.7% | 76.7% | 100.0% | 1.00 | 50.0% |
| 0.60 | 6 | 9 | 66.7% | 76.7% | 100.0% | 1.00 | 50.0% |

**结论（synthetic 模式）：** threshold=0.30 完美还原 9 个 GT 簇（100% Purity + 100% Coverage + 0% Over-merge）；threshold=0.45 / 0.60 把语义无关的簇（`batch_image_upload` + `upload_pdf_reliably`）误并入同一簇，Over-merge 飙到 50%。当前默认 `0.45` 在真实 LLM 数据上表现可接受（见 §5.1），但 synthetic 提示在小型数据上 0.30 才是稳定选项；建议大样本时再做 sweep。

报告：`work/eval-results/20261012T113237Z-cluster.json`。

### 5.3 Stub 模式（无外部调用）

```bash
uv run --with pgserver==0.1.4 python scripts/run_clustering_e2e.py --stub-llm --threshold 0.30
```

输出：12/12 分类、6 个 algorithmic 簇（threshold=0.30），全部经 stub 复核被命名；总耗时 < 0.1s，无任何外部 HTTP。

---

## 6. 安全与边界

- **Raw 保护**：`feedbacks.content` 在 Phase 2 全程只读；不写、不重写、不级联删除；`feedback_analyses.semantic_text` 是新字段，与 `content` 解耦。
- **凭证**：`api_key` 不出现在任何日志、错误消息、JSON 报告；`EMBEDDING_API_KEY` / `LLM_API_KEY` 仅从 `Settings`（env 变量或 `backend/.env`）读取，从不接受函数参数。
- **错误出口**：LLM/Embedding 客户端按状态码分类（401/403 → 认证，4xx → 客户端错误，5xx/429 → 临时），不回显 body；`analysis_runs.error_message` 写入截断后的 `repr(exc)` + traceback 末行，不含凭证。
- **跨批隔离**：Phase 2 走相同的 `pg_advisory_xact_lock` 保护（Phase 1 已加），并发的 retry 走 `UPDATE ... WHERE status IN (eligible) RETURNING id` 原子抢占。
- **多模型安全**：`UNIQUE(feedback_analysis_id, embedding_model)` + 维度校验避免同 analysis 写入不同模型的向量而误用。

---

## 7. 验证边界

- 阈值扫描的 **synthetic** 模式用 `gt_intent` 派生 `semantic_text`，本质是 Phase 1 已被"对齐到 GT"的乐观上界；**full** 模式会因 Phase 1 噪声而指标更低（标签未人工复核）。
- 12 条样本不构成业务准确率结论；500 条正式评测集不在 Phase 2 范围。
- LLM 复核是 **best-effort**：一次 LLM 失败 run 标 `partial_failed`，但 algorithmic 簇仍然可见。
- Docker / PostgreSQL 17 容器未实测；测试与脚本使用 pgserver 启动 PostgreSQL 16.2。
- Outlier 簇目前是 LLM 主观判断；缺业务规则或人工复核兜底。
- 未做"多语言 / 中文 / 跨域 embedding"实验；本地 embedder 是 SHA-256 哈希桶，仅用于测试，真实部署应配置 `EMBEDDING_*` 走远程模型。
- 阈值 0.45 是 Phase 1 兼容默认；synthetic 实验提示在小型数据 0.30 更稳定，但生产阈值需大样本 sweep 确认。
- **真实语义 Embedding 验证未做**：当前环境已配置分类/复核 LLM 凭据但未发现真实 Embedding 服务凭据；所有 E2E / 评测均使用 `LocalHashEmbedder`（128 维 SHA-256 哈希），仅验证算法管道端到端通畅，不构成"语义相似度正确"的证据。如提供 Embedding 模型名称 + 服务地址 + 密钥，可立即补跑 `run_clustering_e2e.py` 真实 Embedding 模式并把指标计入交付。

---

## 8. 自动化验证

```bash
cd backend
uv sync --locked
uv run --with pgserver==0.1.4 --with pytest==8.3.3 --with pytest-asyncio==0.24.0 \
    pytest -q
uv run --with ruff==0.6.9 ruff check app/ tests/ scripts/

# 真实 PostgreSQL 全量 + 迁移往返
uv run --with pgserver==0.1.4 python scripts/verify_postgres.py

# 阈值扫描（无 LLM）
uv run --with pgserver==0.1.4 python scripts/run_clustering_eval.py --mode synthetic

# 真实 LLM 端到端
LLM_API_KEY=... LLM_BASE_URL=... LLM_MODEL=MiniMax-M3 \
uv run --with pgserver==0.1.4 python scripts/run_clustering_e2e.py --threshold 0.45
```

---

## 9. Phase 2 交付物

- 新增四张表 + 4 个新模型 + 7 个新 API + 26 个新测试 + 2 个评测脚本（synthetic threshold sweep + 真模型 E2E）。
- 数据归档：所有 `work/eval-results/*-cluster.json` 与 `*-e2e.json`。
- 文档：`docs/08` 追加 Phase 2 决策、`docs/09` 追加 Phase 2 验证、`docs/16` 本报告。
- 不触碰 Insight / Evidence / Opportunity / Frontend / PRD / MCP / Multi-Agent。

---

## 10. 人工 Gate

**Phase 2 技术交付完成，等待三文鱼亲自复测并明确批准。** 在此之前：
- 禁止开始 Phase 3。
- 禁止修改 Raw feedback、Phase 1 已有测试、Phase 1 迁移、Phase 1 prompt。
- Phase 2 业务代码可继续微调（如阈值、prompt 措辞），但任何破坏性改动须走新一轮独立复检。

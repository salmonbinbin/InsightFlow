# 数据库与 API

## 本次实际实现

数据库：PostgreSQL；ORM：SQLAlchemy 2；驱动：psycopg 3；迁移：Alembic 0001。
仅 projects / feedbacks 在本次创建；下方其余 7 张表为后续 Sprint 1 设计，不在 V0.1 迁移内。避免在模型维度未选定前固定 pgvector 列。

projects：UUID 主键；name/product_name 各 200 字必填；description 可空；created_at 为服务端 UTC 时间。
feedbacks：UUID 主键；project_id 外键；external_id 200 字；content 原文；source/user_type 100 字；user_id 200 字；feedback_created_at 可空；created_at 为导入 UTC 时间。

约束：UNIQUE(project_id, external_id)，project_id 索引。外键阻止孤立反馈，不提供删除/更新 Raw Feedback 的 API。不可变是本版 API 合约，尚未配置数据库触发器阻止管理员手动修改。

## 已实现端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /health | 进程存活 |
| GET | /health/ready | 数据库及两张业务表可查询 |
| POST | /projects | 创建项目，201，name/product_name 必填 |
| GET | /projects | 列表，limit 1–100，offset ≥0 |
| GET | /projects/{project_id} | 项目详情 |
| POST | /projects/{project_id}/feedback/import | multipart/form-data 的 file 字段 |
| GET | /projects/{project_id}/feedback | 分页，limit 1–200，默认50，offset 默认0 |

导入成功响应：`{"total":12,"imported":12,"skipped":0}`。
查询响应：`{"total":12,"items":[...],"limit":50,"offset":0}`。

## CSV 合约

- UTF-8，可带 BOM；逗号分隔；支持带引号逗号和换行。字段名精确匹配，不在本版提供映射界面。
- 必填列 feedback_id、content；可选 source、user_id、user_type、created_at；不接受其他列，防止把 ground truth 当成业务输入。
- ID 和可选文本去掉首尾空格；content 保留原文空白，但不能为空白。content 最长20000字。可选空值存 NULL。
- 日期格式 YYYY-MM-DD 或 ISO 秒级时间，可含微秒、Z 或时区偏移；无时区按 UTC 处理。源日期映射为 feedback_created_at，导入时间单独记录。
- 单文件 ≤10 MiB，≤10000 条；超大小413，非CSV415，格式/行错误422（detail.message + detail.row，row 为物理行号，多行字段指结束行）。
- 校验全部完成才写库；格式错误整批不写。文件内相同 ID 相同值允许跳过，不同值422。
- 已入库同项目同 ID 同数据跳过；数据不同409，整批回滚；不会覆盖原反馈。用户数、反馈数统计后续基于去重后的数据库记录。
- 跨项目相同 external_id 可以存在；不存在项目404，UUID格式错误422。
- PostgreSQL 唯一约束及 ON CONFLICT 抵御并发重复插入；一事务内提交，冲突时回滚。

本版是单用户本地后端，不含认证。运行默认只监听127.0.0.1；公开部署需另外完成鉴权及请求体/并发限制。

## 原讨论最终 Sprint 1 表结构

# 10. Sprint 1 数据库最终版本

### projects

```text
id
name
product_name
description
created_at
```

### feedbacks

```text
id

project_id

external_id

content

source

user_id

user_type

feedback_created_at

created_at
```

### analysis_runs

```text
id

project_id

run_type

model

prompt_version

status

input_count

success_count

failed_count

input_tokens

output_tokens

latency_ms

created_at
```

### feedback_analyses

```text
id

feedback_id

run_id

feedback_type

module

intent

pain_point

sentiment

severity

confidence

semantic_text

created_at
```

### feedback_embeddings

```text
id

feedback_analysis_id

embedding_model

embedding

created_at
```

### clusters

```text
id

project_id

run_id

name

summary

feedback_count

unique_user_count

average_severity

negative_ratio

confidence
```

### cluster_members

```text
cluster_id

feedback_analysis_id

similarity_score
```

### insights

```text
id

cluster_id

title

pain_point

affected_segment

insight

recommendation

confidence

created_at
```

### insight_evidence

```text
insight_id

feedback_id

relevance_score
```

这就是 Sprint 1 的核心数据库。

---

## 后续表的约束补充（本次设计建议，未实现）

- analysis_runs.project_id → projects；status 使用四状态枚举，计数非负，重试不重复累计。
- feedback_analyses.feedback_id → feedbacks；run_id → analysis_runs；UNIQUE(run_id, feedback_id)，severity 1–5、confidence 0–1。
- feedback_embeddings.feedback_analysis_id → feedback_analyses；选定模型和维度后创建 pgvector 扩展与列；避免混用不同维度。
- clusters 的 project_id/run_id 必须一致；cluster_members 复合主键，防止重复成员；跨项目/运行一致性需复合外键或事务服务检查。
- insights.cluster_id → clusters；insight_evidence 复合主键；引用必须属于对应簇。外键存在性不等于证据语义支持，另做评测。

## 后续 API（尚未注册）

POST /projects/{project_id}/analysis；POST /projects/{project_id}/clustering；GET /projects/{project_id}/clusters；GET /clusters/{cluster_id}。
新增建议 GET /analysis-runs/{run_id} 用于进度查询；分析请求返回202+run_id，不阻塞到整批完成。Requirement/PRD/人工优先级 API 在 Sprint 2–3 再确定。

## Phase 7 补充：项目级 Read API（D1）

前端 `/prds` 与 `/evaluation` 两个页面在页面刷新后必须从真实后端发现数据，不得使用 `sessionStorage` 或 mock 兜底。新增两个只读的项目级列表端点，承担 Phase 1—6 业务数据的“项目级索引”职责：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /projects/{project_id}/prds | 项目内全部 PRD 版本，按 `version DESC, id DESC` 稳定排序，分页 |
| GET | /projects/{project_id}/evaluations | 项目内全部 EvaluationRun，按 `created_at DESC, id DESC` 稳定排序，分页 |

### 共同契约

- 路径参数 `project_id` 为 UUID；非法格式 → `422`，未知 id → `404`（与项目存在但当前租户无访问权不可区分，避免跨项目探测）。
- 查询参数 `limit`、`offset` 由 FastAPI `Query` 校验：`limit ∈ [1, 200]`，`offset ≥ 0`；越界 → `422`。默认 `limit=50`、`offset=0`。
- 跨项目隔离完全靠 `WHERE project_id = :project_id` 过滤；Phase 7 单用户，无独立鉴权层。
- 响应统一为 `{ "total": int, "items": [...], "limit": int, "offset": int }`；`items=[]` 与 `total=0` 是合法的空项目状态。
- 排序键必须包含 `id` 作为次级键：单事务批量写入或 `DateTime(timezone=True)` 时钟偏移会造成 `created_at`/`version` 同值，仅靠主排序键会导致 `limit/offset` 分页跨页重复或漏行。

### GET /projects/{project_id}/prds

- 实现位于 `backend/app/api/requirement_prd.py:list_project_prds_route`。
- 响应 `items` 元素为完整 `PRDRead`（13-section PRD 内容、版本号、状态、`grounding_warnings`、`is_ai_generated_draft`、token / latency 等度量字段随行）。
- 返回**每一个版本**而不是每个 Requirement 的最新版本。前端按 `requirement_id` 分组渲染版本时间线；如果只保留最新一行会静默丢弃更早的 draft 与 accepted 版本，破坏审计链。
- `status` 等运行期字段保持原始字面量；如需做状态机判断由前端按 `draft / reviewing / accepted / archived` 自行解析。
- 实际 PRD 创建仍走既有 `POST /requirements/{requirement_id}/prds`、`GET /requirements/{requirement_id}/prds` 等端点；本接口只是项目级索引。

### GET /projects/{project_id}/evaluations

- 实现位于 `backend/app/api/evaluations.py:list_project_evaluations`。
- 响应 `items` 元素为完整 `EvaluationRunRead`（含 `dataset_label_status == "synthetic_reference_labels_unreviewed_no_human_review"` 标记，提醒前端该评估对象为未审核的演示合成数据）。
- 每次 EvaluationRun 的实际度量与逐 case 记录仍由既有 `GET /evaluations/{id}/metrics` 与 `GET /evaluations/{id}/cases` 提供；项目级列表只承担"运行索引"职责。
- `created_at` 默认是 ORM 服务端 UTC 时间；批量提交时同一事务内多行可能同时间戳，故稳定排序依赖 `id DESC` 次级键。

### 测试矩阵（`backend/tests/test_phase7_project_lists.py`）

每个端点覆盖五条不变式：

1. 空项目 → `{"total": 0, "items": [], "limit": 50, "offset": 0}`，绝不返回 `items=null` 或 5xx。
3. 跨项目请求：A 项目的 PRD / EvaluationRun 永不通过 B 项目的请求泄漏。
4. `limit` / `offset` 翻页：`limit=2, offset=0/2/4` 跨页并集 = 全部种子 id，任意两页交集为空。
5. 响应字段集：每个 `items` 元素至少包含 `PRDRead` / `EvaluationRunRead` 全部键；前端 TypeScript 类型可静态信任该 schema。
6. 边界：`limit=0 / 201 / 负值` 与 `offset=-1` → `422`。

实现依赖的 ORM 链：`Project → AnalysisRun → Cluster → InsightRun → Insight → InsightDecision → Requirement → PRD`，以及 `Project → EvaluationRun`。PRD 测试用例在 fixture 内直接插入 ORM 行，绕过 Phase 5 AI 客户端与 Phase 6 评估运行器——失败信号指向项目列表端点本身，而非上游生成路径。

## Phase 7 补充：Analysis / Feedback / Evidence 只读接口

| 方法 | 路径 | 当前契约 |
| --- | --- | --- |
| GET | `/projects/{project_id}/analysis-runs?run_type=classification\|clustering&limit=&offset=` | 项目内分类/聚类 Run 历史；`run_type` 可省略；按 `created_at DESC, id DESC` 排序 |
| GET | `/projects/{project_id}/insight-runs?limit=&offset=` | 项目内洞察 Run 历史；同样稳定排序 |
| GET | `/projects/{project_id}/feedback/{feedback_id}/analysis?run_id=` | 指定 Run 或最新已完成分类 Run 对应的单条反馈 AI 分析；原始反馈与分析分离 |
| GET | `/insights/{insight_id}/evidence` | 从数据库回填 Evidence 对应 Raw Feedback 原文、来源及分类分析字段 |

列表响应均为 `{total, items, limit, offset}`，默认 `limit=50, offset=0`，`limit` 范围 1—200。不存在的项目、跨项目反馈或不属于项目的 Run 返回 404；空项目返回空列表。`feedback/{id}/analysis` 不写入 Raw Feedback。详情的项目隔离由后端查询和前端项目切换时请求取消共同保障，单用户 Phase 7 尚无独立身份鉴权层。

Evaluation 详情 `GET /evaluations/{id}`、`/metrics`、`/cases` 支持 `project_id` 约束；metrics/cases 前端按响应 `total/limit/offset` 翻页，不能把第一页误称全量。PRD 生成继续使用既有 `POST /requirements/{id}/prds`；Phase 7 修正工具协议有界重试，内容进入持久化前仍须通过 13 节、Evidence、六类 Edge Case、Business Commitment 与 `acceptance_criteria.verifiable=true` 校验。当前 PRD Prompt 版本 `prd-generation-v3`，历史 v1/v2 文件保留。

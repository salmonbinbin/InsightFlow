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

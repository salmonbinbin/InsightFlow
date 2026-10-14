# Phase 5 Delivery Report — Requirement + PRD Draft

日期：2026-10-14（Asia/Shanghai）。负责人：三文鱼。

## Status

**PARTIAL。** Phase 5 后端工程实现与隔离环境验收通过。三文鱼提供的 `key/.env` 已用于真实 MiniMax-M3 调用：Requirement 客户端取得结构化响应；PRD 客户端两次取得响应，但分别被业务校验和严格字段校验拒绝，未完成真实 AI 草稿入库。CSV 到 PRD 的真实模型全链路及 3～5 份真实生成文档的人工语义抽查仍为 **NOT MEASURED**。这份文件是 Claude 配置 JSON，不能直接作为后端 dotenv 读取；本次只在隔离验收进程中读取，未修改原文件或写入密钥。Phase 2 真实语义 Embedding 和 Phase 3 人工语义复核限制继续保留。

## What Changed

- 新增 Requirement 和 PRD 独立数据层，迁移 `0010`；不改写 Raw Feedback、Insight/Evidence 或 Phase 4 决策。
- Requirement 仅允许有 Evidence 且 PM Decision 为 `accepted`/`overridden`、PM Priority 非空的 Insight 创建。Priority 继承 PM 值；每个 Insight 只允许一条 Requirement，重复创建返回冲突。
- Requirement 支持 AI Structured Output 或 PM 输入、读取、编辑、状态转换、项目内按状态分页查询；记录来源/决策/评分快照、证据引用、模型、Prompt、tokens 与延迟。
- PRD 为结构化 13 节草稿，支持生成、读取、编辑、再生成新版本、历史分页及 Accept；历史响应返回全局 active 版本指针。旧版本与 PM 编辑保留，Accept 记录时间；因无认证系统，`accepted_by` 留空且不接受客户端伪造身份。
- Prompt 固定为 `requirement-generation-v1` 与 `prd-generation-v1`。结构和业务校验拒绝未知模型字段、未知证据 ID、无依据的常见商业结果承诺；PRD 要求六类 Edge Case、可验证的 Acceptance Criteria、MVP In/Out Scope。关键词校验不能证明整体语义 groundedness。

## Files Changed

代码：`backend/app/db/config.py`、`backend/app/main.py`、`backend/app/models/__init__.py`；新增 `backend/alembic/versions/0010_phase5_requirement_prd.py`、`backend/app/models/requirement_prd.py`、`backend/app/schemas/requirement_prd.py`、`backend/app/api/requirement_prd.py`、`backend/app/services/requirement_prd.py`、`backend/app/services/requirement_prd_client.py`、两个 Phase 5 Prompt 及 `backend/tests/test_phase5.py`。文档：本报告、`工作记录.md`、`docs/08`、`docs/09`、`AGENT_HANDOFF.md`、`README.md`。未修改 0001—0009、Phase 1—4 旧测试/Prompt/业务逻辑或前端。

## Database Migrations

`0010` 只新增 `requirements` 与 `prds`，含 FK、状态 CHECK、版本/来源约束及查询索引。独立 PostgreSQL 上 base→head→base→head、Alembic check 全部通过；带数据 `0010→0009→0010` 后 Raw 原文不变。回退会删除 Phase 5 表及其数据，只能用于隔离验收环境，不可对用户现有数据直接执行。

## APIs Added

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/insights/{insight_id}/requirement` | 创建 Requirement；空请求体触发 AI，`content` 为 PM 输入 |
| GET/PATCH | `/requirements/{requirement_id}` | 读取、编辑、状态变更 |
| GET | `/projects/{project_id}/requirements` | `status`、`limit`、`offset` 筛选与分页 |
| POST/GET | `/requirements/{requirement_id}/prds` | 生成新版本、查历史及 active 指针 |
| GET/PATCH | `/prds/{prd_id}` | 读取、编辑未接受草稿 |
| POST | `/prds/{prd_id}/accept` | 人工接受，重复请求幂等 |

## Requirement Design

保存 `insight_id`、`decision_id`、`score_id`、来源哈希、AI/PM Priority 快照及证据引用；状态为 `draft/reviewing/approved/rejected/archived`。AI 输出先做 Structured Output、Schema、Evidence ID 和商业断言校验，再写库。无可信 baseline 时指标只能是监控定义，不生成目标值。已知风险与推测风险分开。

## PRD Structure and Versioning

结构为 Background、Problem、Target User、User Story、Product Goal、Proposed Solution、Functional Requirements、User Flow、Edge Cases、Acceptance Criteria、Success Metrics、Risks、MVP Scope。每次 POST 创建新版本，唯一约束为 `(requirement_id, version)`；PATCH 草稿保留修改，Accept 后内容不可变。当前 active 定义为该 Requirement 最高版本，不随历史分页变化。状态为 `draft/reviewing/accepted/archived`；AI 生成结果始终先是 Draft。

## Supervisor → Worker

Codex 负责范围、独立测试、文档与本地 Git；Claude Code 只写 Phase 5 白名单实现与新增测试。全程单一写入者；所有返工续接同一 `threadId=ff658918-26c0-46fc-af70-e575ac4ec613`。jobId 依次为 `925ce589c72e42c9a5ddadd03519067f`、`792839cc434c47c39435f22712aa26ad`、`4577a4c5a3d249f09b609efa57b0dbec`、`e20201e5c2914a8f9dcd3682bd309009`、`d3b93ee0b0e4434483366f10f793044c`、`52f84884078e43ada8b57a0b3f0874a5`、`a282c2da5b764b7b98b65c752ff7cb18`。首两次触及桥接 600 秒上限；Codex 在任务停止后复验并提供失败证据，后续同会话修复。Claude 未执行 Git。

## Tests Added and Results

Phase 5 新增 107 个专项测试，覆盖门禁、Priority 继承、严格输出、Evidence/商业断言、人工编辑、六类 Edge Case、版本历史、重复请求、异常与 HTTP 状态。Codex 最终独立复验：

| 检查 | 实际结果 |
|---|---|
| `uv run pytest -q` SQLite | **417 passed, 5 skipped** |
| 隔离 PostgreSQL 16.2 全量 | **422 passed**；`POSTGRES_VERIFICATION_PASSED` |
| `uv run ruff check app tests alembic scripts` | PASS |
| Phase 5 文件 `ruff format --check` | 9 files already formatted |
| Alembic head/check 与 fresh 往返 | `0010`；PASS |
| 带数据 `0010→0009→0010` | PASS；Raw 原文不变 |
| 真实 TCP HTTP、隔离 PostgreSQL、PM 输入 | PASS：Requirement 编辑、PRD v1 编辑、v2、历史 active 指针、重复 Accept、Raw 与决策保护 |
| 真实 AI Requirement 客户端 | MiniMax-M3 成功返回 8 个必需顶层字段及受控 Evidence ID；1658/1085 tokens，约 16.7 秒。未走数据库持久化与完整业务校验 |
| 真实 AI PRD 客户端 | 两次均收到模型响应，但一次在 `mvp_scope.out_of_scope` 触发无依据商业承诺校验（3044/2996 tokens，约 39.8 秒），一次因额外顶层字段 `item` 被拒；真实 AI PRD 入库未通过 |
| CSV 起点完整真实 AI E2E | NOT MEASURED |
| 3～5 份真实文档人工语义抽查 | NOT MEASURED |

## Real End-to-End Result

隔离 PostgreSQL 的真实 TCP 服务验证了从**受控已有 Insight/Evidence/PM Decision**到 PM 输入 Requirement、PRD v1、PM 编辑、PRD v2、历史查询与 Accept 的链路：`v1/v2=[1,2]`，编辑保留，重复 Accept 时间不变，Raw 与 Phase 4 决策快照不变。此链路没有调用 AI，也没有从 CSV 执行真实分类/Embedding/聚类；不能称为用户要求的完整真实 AI E2E。

## Sample Requirement and PRD Summary

真实模型 Requirement 客户端用一条受控合成的“多图选择”反馈取得结构化响应，引用了给定 `sample-1`。该响应没有进入服务层或数据库，不能据此认定 Requirement 业务验收通过。隔离 HTTP 用的 PM 输入样本为“图片选择效率”，Priority `P1` 来自受控 PM Decision；PRD 仅用于验证 13 节 Schema 和版本/编辑链路。这些样本不代表真实用户反馈或生成质量。

## Known Issues and Technical Debt

- 已有可用的 MiniMax-M3 Anthropic 兼容配置，真实模型往返已测；PRD 两次输出均未通过后端校验，模型输出稳定性、真实草稿入库和完整端到端尚未通过。
- Evidence ID 校验与常见商业承诺拦截是确定性保护，不能证明 Problem、User Story、验收标准整体语义正确；需要真实输出及三文鱼人工复核。
- Phase 2 真实语义 Embedding、Phase 3 正式 Groundedness/Hallucination 与人工复核、Phase 4 正式业务评分质量仍未关闭。
- 本阶段沿用同步 HTTP/模型调用和单用户本地后端；未增加认证、前端、自动发布或 Phase 6 功能。

## Git Commit

Phase 5 工程实现已本地提交为 `600ba0b feat: add requirement and versioned prd workflow`，未 push。本次真实模型补验与报告修订单独记录。

## User Retest Steps

1. 在本地配置数据库并升级至 `0010`，按 README 启动后打开 `http://127.0.0.1:8000/docs`。若要测试 AI 空请求体生成，先在本地安全配置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`，不要把密钥写入 Git 或聊天。
2. `GET /projects/{project_id}/insights` 选有 Evidence 的 Insight；`GET /insights/{insight_id}/decision` 确认 `accepted/overridden` 和 PM Priority；`GET /insights/{insight_id}/evidence` 查看来源。
3. `POST /insights/{insight_id}/requirement` 传 `{}` 生成 AI 草稿；`GET /requirements/{id}` 看 Insight、Decision、PM Priority、Evidence；`PATCH /requirements/{id}` 传完整 `content` 编辑，再 GET 核对持久化。
4. `POST /requirements/{id}/prds` 传 `{}` 生成 v1；`GET /prds/{v1_id}` 看结构、来源与 Draft；`PATCH /prds/{v1_id}` 传完整 `content` 编辑，再 GET 核对。
5. 再次 `POST /requirements/{id}/prds` 生成 v2；`GET /requirements/{id}/prds` 看 `active_version=2`，`GET /prds/{v1_id}` 确认旧版编辑仍在；`POST /prds/{v2_id}/accept` 后再次 GET，确认 `status=accepted`、`accepted_at` 有值、`accepted_by=null`。重复 Accept 不应改变时间。

## Next Recommended Phase / STOP

先定位并修复真实模型 PRD 输出的字段/商业断言问题，再完成真实 AI Requirement+PRD 入库、CSV 全链路与 3～5 份人工抽查，由三文鱼复测 Phase 5。**STOP：不进入 Phase 6、前端、MCP、Jira、Notion 或 Multi-Agent。**

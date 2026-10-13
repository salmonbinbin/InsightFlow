# Phase 4 监督实施与验收方案

日期：2026-10-13。负责人：三文鱼；Supervisor：Codex；Worker：Claude Code。

## 授权、基线与历史限制

三文鱼已明确授权 Phase 4 Opportunity Score + Human-in-the-loop Product Decision 的开发监督、独立验收和本地 Git 提交；禁止 push，交付后 STOP，不进入 Phase 5。

开发前基线位于提交 `db1694c`：SQLite `225 passed / 5 skipped`，Ruff 全范围通过；隔离 PostgreSQL 16.2 下 `230 passed`，Alembic `upgrade/check → downgrade base → upgrade/check` 完整通过。Phase 2 的真实语义 Embedding 尚未验收，Phase 3 工程验收 PASS 但正式 Groundedness/Hallucination 和负责人复核仍未关闭。Phase 4 可以对 `draft` 与 `needs_validation` 洞察评分，但必须返回并保存来源洞察状态；评分或 PM 决策不构成上游语义验收通过。

## 目标与边界

本阶段只实现：确定性 Opportunity Score、可解释维度、AI 辅助 User Value、集中权重与阈值、PM Business Value、AI Priority、PM 接受或覆盖、决策状态及历史。

禁止实现 Requirement、PRD、PRD Editor、Jira、Notion、MCP、Research Agent、Multi-Agent、正式前端或重型队列；不修改 Raw Feedback、历史迁移 0001—0008、Phase 1—3 Prompt、已有业务测试或已有 API 契约，不增加依赖。

## 架构选择

采用“不可变评分快照 + 当前决策 + 追加事件历史”：

1. `opportunity_scores` 保存每次计算的来源统计、维度得分、可用性、实际权重、阈值、总分、AI Priority、AI User Value 解释、模型/Prompt/计量、公式版本及来源快照哈希。历史行不更新。
2. `insight_decisions` 每个 Insight 一行，保存当前 Business Value、当前引用的评分、AI Priority、PM Priority、决策状态和乐观版本。
3. `insight_decision_events` 追加记录每次 Business Value 变更、接受、覆盖、延期或拒绝；记录请求幂等键和版本，禁止覆盖历史。

不采用单行覆盖方案，因为它无法解释历史评分和多次 Priority 修改；不采用完整事件溯源重建所有当前状态，因为对本阶段过重。

## 数据与评分公式

### 统一量纲

所有维度与 Business Value 使用 `0.0—10.0`。默认权重为：

- User Coverage：0.25
- Severity：0.25
- Growth Trend：0.15
- User Value：0.20
- Business Value：0.15

请求可传完整五维权重覆盖默认值；各值必须有限、非负且总和为 1（允许十进制舍入误差 `1e-9`）。实际权重写入每个评分快照。默认值集中在 Phase 4 配置模块，不在路由、服务和测试之外重复实现业务判断。

### User Coverage

优先使用来源 classification run 快照中的非空 `user_id`：

`affected_unique_users / project_unique_users × 10`。

分子只统计 Insight 来源成员，分母只统计同一 classification run 输入快照，避免后来导入数据改变旧洞察分母。若没有任何可靠 `user_id`，该维度标记 `unavailable`，不伪造用户覆盖率。

### Severity

读取 Insight 来源成员在绑定 classification run 中的 Phase 1 `severity`：

`raw_severity = (average_severity - 1) / 4 × 10`。

为避免单条极端反馈直接得到满分，使用 `reliability = min(1, feedback_count / 3)`，最终：

`severity_score = raw_severity × reliability`。

公式确定、可解释、可单元测试；不让 LLM 重算 Severity。

### Growth Trend

只使用非空 `feedback_created_at`。以来源成员的最新时间为锚点，比较最近 30 天与之前 30 天。两个窗口都有可靠数据且前一窗口计数大于 0 时：

`growth_rate = (recent_count - previous_count) / previous_count`

`growth_score = clamp(5 + 5 × clamp(growth_rate, -1, 1), 0, 10)`。

时间覆盖不足、前一窗口为 0 或有效时间太少时标记 `unavailable`。Growth unavailable 时从分母排除其权重，对其他可用非 Business 维度重新归一化；返回 `effective_weights`，不填中性假值。

### User Value 与 AI 输出

AI 只根据当前 Insight、数据库证据摘要和确定性统计输出：

- `user_value_score`：0—10 有限数；
- `reasoning`：有长度上限的证据约束说明。

使用版本化 Prompt `opportunity-recommendation-v1` 和单一 Structured Output 工具。禁止输出或声称收入损失、流失比例、市场规模、留存变化及无证据人群。输出先经过 JSON/工具协议、Pydantic 与业务范围校验，再允许写入评分快照。模型失败或输出非法时不写半成品评分，API 返回安全错误，不回显上游正文或密钥。

### Business Value、总分与 Priority

Business Value 必须由 PM 输入；未填写时评分可保存维度预览，但 `total_score`、`ai_priority` 均为 `null`，Decision 保持 `pending_review`。不得使用默认 Business Value。

Business Value 可用后，Growth/Coverage 等普通缺失维度按上述规则排除并对其余可用权重归一化；Business Value 不可排除。总分使用 `Decimal` 计算并四舍五入到两位，范围强制为 0—10。

Priority 阈值集中配置并写入快照：

- `0.00 ≤ score < 5.00`：P2
- `5.00 ≤ score < 7.50`：P1
- `7.50 ≤ score ≤ 10.00`：P0

`ai_priority` 是确定性公式的系统建议，LLM 不得绕过公式另写优先级。`ai_reasoning` 由公式 breakdown、实际权重和 AI User Value 依据组成。

计算版本固定为 `opportunity-score-v1`。权重、阈值、缺失策略或公式变化必须新增版本，不能改写旧评分解释。

## Human-in-the-loop

Decision 状态：`pending_review / accepted / overridden / deferred / rejected`。

- 更新 Business Value：自动创建新 Opportunity Score 快照，并把当前 Decision 指向新评分；状态回到 `pending_review`，不继承旧接受结论。
- Accept：要求当前评分完整，把 `pm_priority` 设为当前 `ai_priority`，状态 `accepted`。
- Override：要求完整评分、`pm_priority` 与 `ai_priority` 不同，并要求受控原因。
- Deferred / Rejected：保留当前 AI Priority，可选 PM Priority，不把产品决定伪装为 AI 判断。

Override Reason：`strategic_priority / core_user_experience / revenue_impact / development_cost / management_decision / data_quality / other`。选择 `other` 时 `override_note` 必填；所有 note 限长并去空白。PM 的人工商业判断可记录，但系统不得把它反写成原始数据事实。

每次 PATCH 必须携带 `request_id`（UUID）和 `expected_version`。同一 Insight + request_id 返回原结果，避免重复事件；版本不一致返回 409，避免并发覆盖。每个事件保存变更前后状态、AI/PM Priority、Business Value、原因、备注和引用评分。

## API

- `POST /insights/{insight_id}/opportunity-score`：创建不可变评分快照；可传完整 weights，默认读取当前 Business Value。
- `GET /insights/{insight_id}/opportunity-score`：读取最新评分及 breakdown。
- `GET /insights/{insight_id}/opportunity-scores`：分页读取评分历史。
- `GET /insights/{insight_id}/decision`：读取当前 PM 决策和版本。
- `PATCH /insights/{insight_id}/decision`：更新 Business Value、接受、覆盖、延期或拒绝。
- `GET /insights/{insight_id}/decision/history`：分页读取追加事件。

不存在资源返回 404；来源不一致、无 Evidence、状态或乐观版本冲突返回 409；请求字段、权重、数值与枚举非法返回 422；模型依赖不可用返回 503；内部栈与密钥不暴露。

## 修改范围

Worker 可以新增 Phase 4 专用的 model/schema/service/client/prompt/API、迁移 0009 和 `test_phase4.py`，并对 `models/__init__.py`、`main.py`、`db/config.py` 做最小注册修改。Worker 不修改文档、AGENTS、旧测试、旧迁移、旧 Prompt、Phase 1—3 业务文件、依赖或前端。

Codex 在 Worker 停止写入后独立检查 diff、运行验收并更新工作记录、验证记录、决策记录及最终交付报告。失败证据必须续接原 Claude `threadId` 修复。

## 验收标准

1. 公式单测覆盖 0/10、精度、权重和、4.9/5.0/7.4/7.5/10、缺失 Growth/Coverage、小样本 Severity。
2. 覆盖完整计算、Business Value 缺失/更新、权重重算、历史评分保留、AI Structured Output 与安全失败。
3. 覆盖 Accept、P1→P0、P0→P2、非法 Priority、缺原因、other 无 note、Decision History、幂等请求、乐观并发和 AI Priority 不变。
4. 覆盖项目/Insight/来源隔离、Evidence 缺失、Raw 不变、来源状态返回、旧评分与事件不可覆盖。
5. SQLite 全量、Ruff、格式、编译通过；隔离 PostgreSQL 全量通过。
6. 0008→0009→0008→0009 带数据往返及 fresh base→head→base→head 通过；模型、约束、索引、外键一致，Phase 1—3 数据保留。
7. 真实 TCP HTTP 和至少一个受控真实模型 Phase 4 链路通过；若上游使用合成或 local hash，必须明确不能关闭 Phase 2/3 质量缺口。
8. 人工抽查 5—10 个实际评分，核对输入、breakdown、Priority、Business Value 重算、AI Priority 保留与 PM 历史。正式语义成绩无标签时写 `NOT MEASURED`。
9. 工作树仅包含 Phase 4 预期修改；无 TODO/FIXME/debug、密钥、临时数据库或无关依赖。Codex 创建本地提交，不 push，随后 STOP。

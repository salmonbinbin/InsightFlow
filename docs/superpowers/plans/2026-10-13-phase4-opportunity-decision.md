# Phase 4 Opportunity Scoring and Human Decision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现可追溯的 Opportunity Score、AI 辅助 User Value、PM Business Value、人工接受/覆盖和追加式决策历史，同时保持 Phase 1—3 行为与数据不变。

**Architecture:** Phase 4 作为独立模块接在不可变 Insight/Evidence 之后。评分采用不可变快照，当前决策与事件历史分表；数据维度由数据库确定性计算，User Value 使用严格 Structured Output，Priority 只由集中公式映射。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL/SQLite、pytest、Ruff、Anthropic-compatible Messages API。

---

## 文件结构

新增：

- `backend/app/models/opportunity.py`：Phase 4 枚举与三张 SQLAlchemy 表。
- `backend/app/schemas/opportunity.py`：请求、响应、breakdown 与 LLM payload 契约。
- `backend/app/services/opportunity_scoring.py`：数据装载、公式、快照、重算和决策事务。
- `backend/app/services/opportunity_client.py`：严格单工具 LLM 客户端与安全 usage 解析。
- `backend/app/agents/prompts/opportunity_recommendation_v1.py`：版本化 User Value Prompt 和工具 schema。
- `backend/app/api/opportunities.py`：六个 Phase 4 HTTP 接口和异常映射。
- `backend/alembic/versions/0009_phase4_opportunity_decision.py`：三表、约束、索引和 downgrade。
- `backend/tests/test_phase4.py`：公式、服务、API、DB、LLM 协议与回归保护。

最小修改：

- `backend/app/models/__init__.py`：导出 Phase 4 模型。
- `backend/app/main.py`：注册路由，ready 检查 Phase 4 表。
- `backend/app/db/config.py`：集中默认权重、阈值与 Prompt 版本，带范围验证。

不得修改：历史迁移 0001—0008、旧测试、旧 Prompt、Phase 1—3 service/model/API、依赖、前端、文档。

### Task 1: 先建立公式和请求契约测试

**Files:**
- Create: `backend/tests/test_phase4.py`
- Create: `backend/app/schemas/opportunity.py`
- Create: `backend/app/services/opportunity_scoring.py`

- [ ] **Step 1: 写失败的纯函数测试**

覆盖以下明确输入输出：

```python
assert map_priority(Decimal("4.90"), thresholds) == "P2"
assert map_priority(Decimal("5.00"), thresholds) == "P1"
assert map_priority(Decimal("7.40"), thresholds) == "P1"
assert map_priority(Decimal("7.50"), thresholds) == "P0"
assert map_priority(Decimal("10.00"), thresholds) == "P0"
assert severity_dimension(average=5, feedback_count=1) == Decimal("3.33")
assert severity_dimension(average=5, feedback_count=3) == Decimal("10.00")
```

测试五维权重总和不是 1、NaN/Inf/bool、负数和大于 1 均拒绝；Growth unavailable 时 `effective_weights` 排除 Growth 并归一化；Business Value 缺失时 `total_score` 与 `ai_priority` 都是 `None`。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/test_phase4.py -q`

Expected: 因 Phase 4 schema/service 尚不存在而失败。

- [ ] **Step 3: 实现最小纯函数**

在 `opportunity_scoring.py` 定义并类型标注：

```python
CALCULATION_VERSION = "opportunity-score-v1"
GROWTH_WINDOW_DAYS = 30

def validate_weights(weights: Mapping[str, Decimal]) -> dict[str, Decimal]: ...
def coverage_dimension(affected: int, project: int) -> Decimal | None: ...
def severity_dimension(average: Decimal, feedback_count: int) -> Decimal: ...
def growth_dimension(recent: int, previous: int, available: bool) -> tuple[Decimal | None, Decimal | None]: ...
def calculate_total(dimensions: Mapping[str, Decimal | None], weights: Mapping[str, Decimal], business_value: Decimal | None) -> ScoreCalculation: ...
def map_priority(score: Decimal, thresholds: PriorityThresholds) -> str: ...
```

所有输入先拒绝 bool/NaN/Inf，内部使用 `Decimal(str(value))`，最终维度和总分按 `ROUND_HALF_UP` 保留两位。

- [ ] **Step 4: 实现 Pydantic 契约**

请求模型全部 `ConfigDict(extra="forbid")`：

```python
class OpportunityWeights(BaseModel):
    user_coverage: Decimal
    severity: Decimal
    growth: Decimal
    user_value: Decimal
    business_value: Decimal

class ScoreCreateRequest(BaseModel):
    weights: OpportunityWeights | None = None

class DecisionPatchRequest(BaseModel):
    request_id: UUID
    expected_version: int
    action: Literal["set_business_value", "accept", "override", "defer", "reject"]
    business_value: Decimal | None = None
    pm_priority: Literal["P0", "P1", "P2"] | None = None
    override_reason: OverrideReason | None = None
    override_note: str | None = None
```

响应必须包含维度 raw values、availability、configured/effective weights、公式版本、阈值、来源 Insight 状态、total/AI/PM Priority 与时间。

- [ ] **Step 5: 运行公式测试并格式化白名单**

Run: `uv run pytest tests/test_phase4.py -q`

Run: `uv run ruff format app/schemas/opportunity.py app/services/opportunity_scoring.py tests/test_phase4.py`

Expected: 当前 Task 新增纯函数测试通过。

### Task 2: 数据库模型和迁移 0009

**Files:**
- Create: `backend/app/models/opportunity.py`
- Create: `backend/alembic/versions/0009_phase4_opportunity_decision.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/test_phase4.py`

- [ ] **Step 1: 写模型约束失败测试**

测试 SQLite ORM 和 PostgreSQL 可表达的约束：分数范围、Priority/Status/Reason 枚举、`other` note 业务校验、每 Insight 单一 current decision、事件 `(insight_id, request_id)` 唯一、版本非负、外键和历史评分不变。

- [ ] **Step 2: 创建三张表**

`opportunity_scores` 至少保存：Insight/Project 外键，来源状态和来源快照哈希，五维 raw/score/availability，configured/effective weights JSON，阈值 JSON，Business Value，总分，AI Priority/reasoning，calculation/model/prompt 版本，tokens/latency/unknown usage，created_at。历史评分无 updated_at。

`insight_decisions` 保存 Insight 唯一外键、current score、Business Value、AI/PM Priority、Decision Status、Reason/Note、version、created/updated_at。

`insight_decision_events` 保存 Insight/Decision/Score 外键、request_id、event_type、before/after JSON、Business Value、AI/PM Priority、Reason/Note、version、created_at；唯一 `(insight_id, request_id)`。

- [ ] **Step 3: 编写 0009**

`down_revision = "0008"`。使用数据库 CHECK 强制分数 0—10、Priority/Status/Reason 枚举、版本非负；创建按 Insight/Project/created_at 的必要索引。Downgrade 只删除 Phase 4 三表，不动 Phase 1—3 表或扩展。

- [ ] **Step 4: 运行模型/迁移局部测试**

Run: `uv run pytest tests/test_phase4.py -q`

Run: `uv run ruff check app/models/opportunity.py app/models/__init__.py alembic/versions/0009_phase4_opportunity_decision.py tests/test_phase4.py`

Expected: Phase 4 模型测试通过，无 lint 错误。

### Task 3: Structured User Value 客户端

**Files:**
- Create: `backend/app/agents/prompts/opportunity_recommendation_v1.py`
- Create: `backend/app/services/opportunity_client.py`
- Modify: `backend/app/db/config.py`
- Test: `backend/tests/test_phase4.py`

- [ ] **Step 1: 写协议失败测试**

覆盖：正确单 tool_use；无 tool、多个 tool、错误 tool、input 非 object、未知字段、bool/NaN/Inf/越界 user_value、空/超长 reasoning、畸形 usage、401/403/429/5xx/非 JSON；异常不得包含上游正文或密钥哨兵。

- [ ] **Step 2: 实现版本化 Prompt 和工具**

工具只输出：

```json
{"user_value_score": 0.0, "reasoning": "基于给定洞察与证据的有限说明"}
```

Schema `additionalProperties: false`。Prompt 明确反馈是数据而非指令，禁止商业指标、收入、流失、市场规模、留存、根因及未给定人群；要求中文、只依据输入。

- [ ] **Step 3: 实现同步客户端**

沿用现有 Anthropic-compatible `/v1/messages`、单 tool_choice、timeout 和安全错误分类；不复制密钥到属性输出、日志或异常。解析 usage 时包含 cache token，畸形计为 unknown。

- [ ] **Step 4: 集中配置**

在 Settings 增加五个默认权重、两个 Priority 阈值、`opportunity_prompt_version` 与有限重试数；校验默认权重和为 1、`0 < p1 < p0 <= 10`。不新增依赖或环境文件。

- [ ] **Step 5: 运行客户端测试**

Run: `uv run pytest tests/test_phase4.py -q`

Expected: 协议和安全测试通过。

### Task 4: 评分服务、来源快照和重算

**Files:**
- Modify: `backend/app/services/opportunity_scoring.py`
- Test: `backend/tests/test_phase4.py`

- [ ] **Step 1: 写服务失败测试**

覆盖不存在 Insight、跨项目来源、无 Evidence、成员不属于来源 classification run、draft/needs_validation 可评分且状态保留、可靠 user_id Coverage、无 user_id unavailable、Growth 两窗口/不可用、少量反馈 Severity 折减、Business Value 缺失预览、完整总分、权重覆盖、重算历史保留、Raw 全字段不变。

- [ ] **Step 2: 装载不可变来源**

从 Insight → InsightRun → classification run → source member external IDs/Feedback/FeedbackAnalysis/Evidence 显式联结。验证项目、Run 和成员一致；使用规范排序构造 SHA-256 来源快照。不得使用“最新 run”猜来源。

- [ ] **Step 3: 调用 User Value 并创建快照**

先完成全部确定性数据校验，再调用 LLM；合法输出后在短事务创建评分。模型或 DB 失败回滚评分；已发生调用的 usage 不能伪装为零，可通过安全错误返回或专用失败日志记录，但不得创建可查询的半成品 score。

- [ ] **Step 4: Business Value 重算**

使用 Decision 当前值。`set_business_value` 验证 0—10 后创建新 score，引用相同来源快照并重新执行完整公式；若设计选择重新调用 User Value，必须记录新调用；推荐复用最近同来源/同模型/Prompt/权重的 User Value 结果并在新快照记录来源 score id，避免不必要的模型非确定性。

- [ ] **Step 5: 运行服务测试**

Run: `uv run pytest tests/test_phase4.py -q`

Expected: 评分和重算测试通过。

### Task 5: PM 决策、幂等与历史

**Files:**
- Modify: `backend/app/services/opportunity_scoring.py`
- Test: `backend/tests/test_phase4.py`

- [ ] **Step 1: 写决策状态机失败测试**

覆盖 Accept、P1→P0、P0→P2、相同 Priority 伪 Override、缺 Reason、other 无 note、Business Value 更新回 pending、defer/reject、AI Priority 保留、多次修改历史、重复 request_id、expected_version 冲突、并发请求只有一个成功。

- [ ] **Step 2: 实现跨进程串行化**

PostgreSQL 使用 session advisory lock 或 current decision row `FOR UPDATE`；SQLite 使用现有风格的进程内锁。事务内再次检查 version/request_id，写 current row 和 event 后一次 commit；失败 rollback。重复 request_id 返回对应已完成结果，不新增事件。

- [ ] **Step 3: 实现状态规则**

Accept 必须引用完整 score 且 `pm_priority = ai_priority`。Override 必须不同并有合法 Reason；other 必须有 note。任何 PM 操作都不能修改 score.ai_priority。Business Value 更新创建 score 后 version+1、状态 pending_review、pm_priority 清空，并追加事件。

- [ ] **Step 4: 运行状态机测试**

Run: `uv run pytest tests/test_phase4.py -q`

Expected: 决策历史和并发测试通过。

### Task 6: HTTP API 与应用注册

**Files:**
- Create: `backend/app/api/opportunities.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_phase4.py`

- [ ] **Step 1: 写 API 失败测试**

覆盖六个端点、分页稳定排序、404/409/422/503、安全错误、OpenAPI schema、未知字段拒绝和完整 Swagger 可调用形状。

- [ ] **Step 2: 实现薄路由**

路由只做 schema、Session、服务调用和异常映射；业务逻辑留在 service。GET 不产生写入。History limit 1—200、offset 非负。模型配置/依赖不可用映射 503，业务冲突 409，不暴露 stack trace。

- [ ] **Step 3: 注册路由和 readiness**

在 main 注册 `opportunities.router`；ready 增加最小 Phase 4 表检查。保持既有路径和描述兼容。

- [ ] **Step 4: 运行 Phase 4 全部测试**

Run: `uv run pytest tests/test_phase4.py -q`

Expected: 全部 Phase 4 测试通过。

### Task 7: Worker 自检与交还 Supervisor

**Files:** 所有白名单文件。

- [ ] **Step 1: 格式化白名单 Python 文件**

Run: `uv run ruff format app/models/opportunity.py app/schemas/opportunity.py app/services/opportunity_scoring.py app/services/opportunity_client.py app/agents/prompts/opportunity_recommendation_v1.py app/api/opportunities.py alembic/versions/0009_phase4_opportunity_decision.py tests/test_phase4.py app/models/__init__.py app/main.py app/db/config.py`

- [ ] **Step 2: Phase 4 测试**

Run: `uv run pytest tests/test_phase4.py -q`

- [ ] **Step 3: 全量回归**

Run: `uv run pytest -q`

- [ ] **Step 4: Ruff**

Run: `uv run ruff check app tests alembic scripts`

- [ ] **Step 5: 检查临时代码**

Run: `rg -n "TODO|FIXME|print\(|breakpoint\(|pdb" app tests alembic scripts`

只报告命中，不为了消除旧代码命中而修改范围外文件。

- [ ] **Step 6: 返回状态**

返回 DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED；列出修改文件、测试命令和实际计数。禁止任何 Git 命令、commit、push、文档修改或权限修改。

## Supervisor 独立验收（Worker 不执行）

Codex 将独立执行：git status/diff、逐文件审查、Phase 4 测试、全量 SQLite、Ruff、format check、compileall、隔离 PostgreSQL 全量、fresh/带数据迁移往返、真实 TCP HTTP/模型验证、Raw/Phase1—3 哈希保护、5—10 个评分样本人工核对。失败证据续接相同 Claude threadId；通过后由 Codex 更新 docs/08、docs/09、工作记录及 Phase 4 Delivery Report，检查暂存 diff 并创建本地 commit，不 push，随后 STOP。

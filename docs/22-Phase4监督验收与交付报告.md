# Phase 4 监督验收与交付报告

日期：2026-10-14（Asia/Shanghai）。项目：InsightFlow。负责人：三文鱼。

## Status

**工程验收 PASS；业务质量 NOT MEASURED。** Phase 4 已完成 Opportunity Score、AI Priority 与 PM Human-in-the-loop 决策闭环。正式业务评分合理性和AI理由质量尚无负责人标签，不能宣称质量PASS。Phase2真实语义Embedding未验收、Phase3正式语义人工复核未关闭；对`draft`或`needs_validation`洞察评分不会改变上游状态。

本阶段未push，交付后STOP，不进入Phase5。

## Supervisor → Worker

- Codex确定架构、范围、验收和Git；Claude Code只改白名单实现文件。
- Claude固定使用edit tier、本地wrapper映射的acceptEdits、simple profile；禁止Git、文档、旧迁移、旧测试、依赖和前端改动。
- 所有实现与返工续接同一`threadId=ce5f9fc2-937b-4091-8a47-c4104065c883`。jobId完整保存在本机忽略目录`work/phase4-supervision/session.json`。
- Claude停止写入后，Codex逐文件审查并独立执行测试。Codex在6条样本抽查中发现effective weights显示总和0.99/1.01，提供证据后续接原会话修复；最终新增2项回归。

## What Changed

- 新增不可变`opportunity_scores`、当前`insight_decisions`、追加式`insight_decision_events`。
- 新增0009迁移、Opportunity model/schema/service/client/prompt/API与Phase4测试。
- 实现来源快照、Coverage、Severity、Growth、AI User Value、PM Business Value、加权总分、AI Priority。
- 实现Business Value原子重算、accept/override/defer/reject、request_id幂等、expected_version乐观并发和事件历史。
- readiness纳入Phase4三表；注册路由和模型。没有新增依赖。

## Files Changed

业务与测试白名单：

- `backend/app/db/config.py`
- `backend/app/main.py`
- `backend/app/models/__init__.py`
- `backend/alembic/versions/0009_phase4_opportunity_decision.py`
- `backend/app/agents/prompts/opportunity_recommendation_v1.py`
- `backend/app/api/opportunities.py`
- `backend/app/models/opportunity.py`
- `backend/app/schemas/opportunity.py`
- `backend/app/services/opportunity_client.py`
- `backend/app/services/opportunity_scoring.py`
- `backend/tests/test_phase4.py`

Codex交付文档：`AGENT_HANDOFF.md`、`工作记录.md`、`docs/08`、`docs/09`和本报告。未修改0001—0008、旧测试、Raw模型、Phase1—3 Prompt/业务逻辑、依赖或前端。

## Database Migration

Alembic head=`0009`，down_revision=`0008`。0009只新增Phase4三表、外键、唯一约束、范围/枚举CHECK和查询索引；downgrade只移除Phase4表。fresh base→head→base→head和带Phase1—3数据0008→0009→0008→0009均通过；已有Project、Raw、Analysis、Cluster、Insight、Evidence计数与Raw原文保持。

## APIs Added

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/insights/{insight_id}/opportunity-score` | 新建不可变评分预览/快照 |
| GET | `/insights/{insight_id}/opportunity-score` | 最新评分与breakdown |
| GET | `/insights/{insight_id}/opportunity-scores` | 分页评分历史 |
| GET | `/insights/{insight_id}/decision` | 当前PM决策与版本 |
| PATCH | `/insights/{insight_id}/decision` | BV/accept/override/defer/reject |
| GET | `/insights/{insight_id}/decision/history` | 追加事件历史 |

不存在返回404；无有效Evidence、来源不一致和版本冲突返回409；非法字段/权重/状态返回422；模型依赖或调用失败返回503，不回显上游正文、栈或密钥。

## Opportunity Formula

默认权重：Coverage 0.25、Severity 0.25、Growth 0.15、User Value 0.20、Business Value 0.15；完整五维覆盖权重必须非负且总和在1e-9内为1。

- Coverage：来源成员唯一用户 / classification run输入快照唯一用户 × 10。无可靠user_id则不可用。
- Severity：`(平均severity-1)/4×10×min(1, feedback_count/3)`。
- Growth：以来源成员最新时间为锚，比较最近30天与此前30天；`clamp(5+5×clamp(rate,-1,1),0,10)`。时间不足或前窗为0则不可用。
- User Value：真实模型只输出0—10分和证据约束理由，Structured Output严格校验；不得生成收入、流失、市场、留存等无证据事实。
- Business Value：0—10，仅PM输入；缺失时总分和AI Priority为空。
- 缺失普通维度被排除，其余非Business维度按原比例重新归一化；Business Value原权重保留，无其他可用维度时权重为1。有效权重以全精度字符串保存，和在1e-9内为1。
- 总分使用Decimal并ROUND_HALF_UP为两位。`[0,5)=P2`、`[5,7.5)=P1`、`[7.5,10]=P0`。计算版本`opportunity-score-v1`。

## Human Review Design

每个Insight有一个当前Decision，评分和事件历史不可变。PATCH必须提供UUID `request_id`和`expected_version`：重复request_id返回首次请求结果，不生成第二事件；版本不一致返回409。

PM可设置Business Value、接受AI建议、覆盖Priority、延期或拒绝。Override要求PM Priority不同于AI Priority并给受控原因；`other`必须有note。任何PM动作都保留原AI Priority，Business Value变化会创建新评分并把状态重置为`pending_review`。

## Tests and Verification

| 检查 | Actual |
|---|---|
| Phase4专项 | 85 passed |
| SQLite全量 | 310 passed, 5 skipped |
| PostgreSQL16.2全量 | 315 passed |
| Alembic fresh往返/check | PASS；`POSTGRES_VERIFICATION_PASSED` |
| 带数据0008往返 | PASS；Phase1—3计数和Raw原文不变 |
| Ruff全范围 | PASS |
| 改动Python文件格式 | 11 files already formatted |
| compileall | PASS |
| Git diff/check、范围、TODO/debug/secret检查 | PASS |
| 前端构建 | 不适用：本阶段未开发前端 |
| mypy/pyright | 不适用：项目未配置，不虚构通过 |

## Real End-to-End Result

隔离PostgreSQL、真实TCP HTTP和真实模型链路：POST preview → PATCH set_business_value → PATCH accept → 同request_id重放 → 评分历史 → 决策历史全部通过。

```text
REAL_TCP_HTTP_MODEL_PASSED=True
source_status=needs_validation
model_attempts=1
model_retries=0
preview_user_value=2.00
final_ai_priority=P1
history_events=2
score_snapshots=2
```

该结果证明Phase4调用、校验、存储和HTTP闭环可运行，不证明上游真实Embedding或Insight语义质量。

## Sample Opportunity Scores

以下6条是隔离数据库中的受控工程审计样本，AI User Value使用确定性stub以便人工复算；不是业务质量评测。

| 样本 | 来源状态 | Coverage | Severity | Growth | UV | BV | Total | AI | PM/状态 |
|---|---|---:|---:|---:|---:|---:|---:|---|---|
| S1 low/no growth | draft | 10.00 | 0.00 | N/A | 2.00 | 2.0 | 3.82 | P2 | P2/accepted |
| S2 high growth | draft | 10.00 | 10.00 | 10.00 | 10.00 | 10.0 | 10.00 | P0 | P0/accepted |
| S3 no user ids | draft | N/A | 7.50 | N/A | 6.00 | 6.0 | 6.71 | P1 | P2/overridden |
| S4 small sample | draft | 10.00 | 2.50 | N/A | 8.00 | 8.0 | 6.94 | P1 | —/deferred |
| S5 declining | needs_validation | 10.00 | 5.00 | 2.50 | 5.00 | 4.0 | 5.73 | P1 | —/rejected |
| S6 custom weights | draft | 10.00 | 7.50 | N/A | 7.00 | 7.5 | 8.23 | P0 | P0/accepted |

每条样本均先建preview，再设置BV并执行PM动作：2个评分快照、2个事件；有效权重和在1e-9内为1，AI Priority未被PM覆盖。

## Known Issues and Technical Debt

- Phase2真实语义Embedding未验收；Phase3正式Groundedness/Hallucination及三文鱼人工复核未关闭。
- Phase4业务评分合理性、AI User Value/理由质量没有正式人工标签：**NOT MEASURED**。
- 当前同步模型调用和同步API沿用既有架构；没有重型队列、认证、正式前端或Phase5功能。
- 真实模型单样本成功不等于稳定性或业务准确率结论。

## Git

实现和本报告由Codex在最终检查后创建一个本地Conventional Commit；提交哈希以本报告所在提交的`git log -1`为准，避免文档自引用哈希。禁止push。

## User Retest Steps

1. 按README启动后打开`http://127.0.0.1:8000/docs`，确认数据库已升级到0009。
2. 选一个已有Evidence的Insight，调用POST opportunity-score，检查来源状态、五维breakdown、configured/effective weights；未填BV时total/AI Priority应为空。
3. 查询GET decision，记录version。PATCH action=`set_business_value`，使用新UUID request_id和当前expected_version；确认新评分、总分和AI Priority出现，旧preview仍在历史中。
4. PATCH action=`accept`并使用新request_id/新version；确认PM Priority等于AI Priority、状态accepted。用同request_id重放，确认不增加事件。
5. 选另一个Insight，设置BV后PATCH action=`override`，传与AI不同的PM Priority、受控reason和note；确认AI Priority未变化、PM Priority和事件历史正确。
6. 可继续测试defer/reject、错误expected_version（应409）、`other`无note（应422）和分页历史。

## STOP

Phase4交付后立即停止。没有开发Requirement、PRD、PRD Editor、Jira、Notion、前端扩展或Phase5内容；等待三文鱼人工复测与明确授权。

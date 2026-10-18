> **最新修复与复验（2026-10-17，v5）：Phase8 PARTIAL，Research质量FAIL，未验收、未提交、未推送。** 已实现保守任务/证据覆盖、官方正文、证据ID引用及安全诊断；最终SQLite1092/11skip、隔离PG1103与迁移往返、Ruff、前端154及lint/build通过。原十题为2完整/6局部/2未验证，复杂问题仍未闭合。详见[39号修复与独立验收报告](39-Phase8任务覆盖修复与独立验收报告.md)。以下保留旧阶段快照；36号根因更正仍有效。STOP，不进入Phase9。

> **最新（2026-10-17，v4）：Phase 8 PARTIAL / 未验收 / 未提交 / 未推送。** 最终SQLite972/11skip、隔离PostgreSQL983与迁移往返、Ruff、前端147及lint/build通过；真实十条为5completed/5partial_failed，官方全文核对仅2完整回答、3局部事实、5未验证，Research质量FAIL。详见[本轮分层定位与复验](35-Phase8Research分层定位与本轮复验.md)。STOP，不进入Phase9。下文旧状态为历史记录。

# Phase 8 人工复测流程（2026-10-16）

当前交付 `PARTIAL`。三文鱼复测应使用**新建隔离 PostgreSQL 和测试项目**；不要对已有开发数据执行 downgrade。准备真实 Tavily 与 MiniMax-M3 配置，密钥只放本机进程环境，不粘贴到文档、浏览器或聊天。

1. 启动隔离数据库，迁移到 head，再启动 FastAPI 与 Next.js。确认 `/health/ready` 为 200、前端能列出测试项目。记录实际端口和项目 ID；本报告里的临时 UUID 已随测试服务退出失效。
2. 打开项目 A 的 Insight 详情。预期 Internal Evidence 与 External Research 分区可见，外部来源不混成用户原始反馈；空历史有明确提示。
3. 输入明确的产品、平台与来源要求（例如 “OneDrive mobile 多图手动上传；仅 Microsoft 官方帮助”），点击 Run Research。预期出现 Run 状态、模型、Prompt、耗时、Findings 和 Sources；若结构或来源不足，必须显示 `partial_failed`/`failed` 与 `Not Verified`，不得展示为已证实竞品事实。
4. 对每条 `confirmed` 发现打开其每个引用页面，核对产品、平台、操作步骤、限制和时间语境是否由原文支持；尤其要核对“页面未提到”是否被错误写成“不支持”，以及 Photos App、iCloud.com、Shared Library 是否被混称。记录 Run ID、引用 URL、对应原文、支持/不支持原因。**Codex 已代行最终 v14a/v14b 共十条来源核对：2 条 `completed` 的 OneDrive 窄事实有 Microsoft 官方原文，8 条 `partial_failed` 没有可交付确认发现；整体质量门槛仍未过。不可只看 `Completed`。**
5. 切换到空项目 B。预期 A 的 Insight/Research 不显示；读取 A 的 Research 历史或 Sources 携带 B 的 `project_id` 应为 404。切回 A，数据应恢复。
6. 在已批准的 Requirement 详情点击 Prepare。预期先显示“仅模拟”，冻结 Title、Description、Priority、Source Insight；检查与 Requirement 一致。Cancel 后预期 Run 为 `cancelled`，无 `SIM-...` 结果且无模拟目标。
7. 再 Prepare，并在批准前将 Requirement 状态撤销或归档。Approve 预期 409，adapter 不执行；重新设为 approved 并 Prepare 后，若仅修改名称等内容，批准仍使用人工所见的原冻结快照。此状态修改路径可通过隔离 API 或测试数据操作；不要改用户数据。
8. 对仍 approved 的新 Run 点击 Approve。预期显示 `SIM-...` 和 `simulated=true`，明确无真实 Jira/Notion/飞书对象；重复批准同一 Run，预期相同 external ID 且只有一条模拟目标。并发双批准、批准与取消竞争、同 request ID 并发 Prepare 的事务断言见交付报告中的隔离 PostgreSQL 专项结果。
9. 保存人工判断：逐条 Research 的来源支持结论、误判及截图；Tool 预览与取消/批准结果；如需返工，给出 Run/Requirement ID、URL 和复现步骤。Phase 8 质量门槛未通过前保持 `PARTIAL`、不提交、不推进 Phase 9。

实际隔离测试结果与限制见 [Phase 8 架构验收与交付报告](32-Phase8架构验收与交付报告.md)。


## v4 真实复测补充（2026-10-17）

从项目根目录运行 `uv run --project backend --with pgserver==0.1.4 python backend/scripts/verify_phase8_research_live.py --samples 10`。默认十题保持原问题，隔离临时库；逐次来源、模型结构与校验落work，不由completed自动评语义。完整步骤和最终逐题对照见[本轮报告](35-Phase8Research分层定位与本轮复验.md)。

页面的completed/partial_failed/failed均应显示“回答完整性：未验证”，即便所有发现都是Cited也不能隐藏。最终真实浏览器Run `53061d7e-24c1-401d-98a4-8a5ecc93b953` 已验证调用与隔离；该临时库已关闭，不能直接复用UUID。正式质量仍FAIL，等待三文鱼人工复核或另行授权本阶段质量返工。


## v5复测补充（2026-10-17）

1. 全新隔离库迁移至0014；不得downgrade用户库。旧Run三项报告字段可为NULL，前端必须显示未验证。
2. 保持原十题，记录原问题→TaskPlan→实际查询/前后过滤→官方正文/哈希→模型选择及协议元数据→守卫/覆盖。先看逐项缺口再核对所有confirmed引文，不能以confidence或completed代替整题审核。复杂要求未解析时须显示原问题需人工核对。
3. 原#6可展示步骤已覆盖但手动/自动区别未验证；原#9必须检查手机三选项和Shared Library去向，不能只看前3个顶层步骤。原#3手势故障不能完成手动多图流程。
4. 前端应显示中文缺口，保留“回答完整性：未验证”；切B清理A的报告/来源/发现，API携带B项目ID返回404。
5. 模拟Prepare/Cancel/Approve/重复Approve外，再Prepare后通过合法approved→archived撤销资格，批准应409；不要用不允许的approved→reviewing转换。数据库仅1条SIM目标，取消Run无external_id，Raw全部字段前后相同。
6. 真实Jira/Notion/飞书/MCP未接入，SIM不能当外部交付；本流程执行结果不代签三文鱼正式验收。当前结果与失败Run见[39号报告](39-Phase8任务覆盖修复与独立验收报告.md)。

## 2026-10-17 v6补充流程

以下是v6新增检查。前文v4/v5结果是历史记录，不能替代本轮执行；本轮结果与剩余阻断见[42号报告](42-Phase8业务补齐实施与独立验收报告.md)。不要对用户数据库做降级、权限修改或重启恢复；先核对端口归属，8000是其他项目。配置仅检查存在，key/.env按JSON读取，禁止source或输出值。

1. 在新的隔离项目和真实FastAPI＋生产Next中发起Research。保留原十题分母；中文、同义改写、两产品分析和离线条件另列。查看原问题每个子要求的覆盖及未解析条件，人工确认“程序覆盖”与“回答完整性：未验证”同时保留。
2. 对原#6核对手动四步与自动备份的独立依据；对#7核对禁用相机上传条件、完整八步及含`"+"`的第三步；对#8核对Choose Existing→Photos→浏览/选择→Done完整分支；对#9核对手机三个上传选项及Shared Library去向/版本。真实页面有答案而程序没取到时，记录取证或覆盖失败，不能写“官方没有资料”。
3. 从事实打开原始官方来源，核对引用、平台、入口、数量、版本和条件。没有完整官方正文快照的搜索摘要不得作为v6已确认事实。内部引号只在结构化引用值中原样保存，不对来源或旧引文负例做宽松归一化。
4. 对推断/建议点“查看支持事实”，再从支持事实打开来源。支持项必须属于同Run、同项目、已确认事实；未验证事实、未知ID或其他Run不能出现为支持。分析本身仍须人工复核，不因有支持ID就变成“官方结论”。分类为空应显示本次未形成有依据候选/候选未通过校验，不表示市场没有机会或风险。
5. 切换项目后，原项目的Research、事实及展开状态应清理；原项目Run、来源、发现、Tool直接以另一项目参数读取均须404。回到原项目再核对来源和事实，不用Mock或预造研究结果演示。
6. 从CSV真实分类、聚类、Insight开始，Research后由测试场景明确设置PM Business Value与Accept/Override，再创建真实AI Requirement、生成真实AI PRD。不得用预造approved Requirement替代。检查PRD的Evidence ID确属该Insight，方案与推测风险明确标注，无凭空量化收益，Research来源不得伪装成Raw Feedback external_id。PRD仍为AI草稿，正式接受由三文鱼本人完成。
7. 模拟审批用真实生成的Requirement按draft→reviewing→approved流转：Prepare→Cancel应无目标；再Prepare→Approve，重复Approve必须同一SIM且仅一条目标；再Prepare后合法approved→archived，再Approve应409。SIM仅表示数据库模拟，不是Jira/Notion/飞书/MCP写入。
8. 比较Raw全字段快照，按id对齐记录；TIMESTAMPTZ在两边统一为UTC ISO后比较同一时刻，其余文本字段逐字比较，不删列或只比较数量。保留开始/结束快照、迁移版本、请求和DB目标数。
9. 最后代码重新执行SQLite、全新PG全量与完整迁移往返、Ruff、前端lint/test/build。原题与新增集逐题独立核对官方原文，分别统计完整/局部/未验证及三类误阳性。未进行同代码复验不能宣称稳定；任一阻断保留PARTIAL、不提交。

历史Phase2真实语义Embedding、Phase3人工语义复核、Phase4业务评分、Phase6 Artifact/长期质量与Phase7稳定性限制继续保留。真实外部写入可DEFERRED，不替代本轮Research质量要求。完成或失败交付均STOP，不进入Phase9。

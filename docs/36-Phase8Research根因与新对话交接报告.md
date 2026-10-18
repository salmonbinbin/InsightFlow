> **最新修复与复验（2026-10-17，v5）：Phase8 PARTIAL，Research质量FAIL，未验收、未提交、未推送。** 已实现保守任务/证据覆盖、官方正文、证据ID引用及安全诊断；最终SQLite1092/11skip、隔离PG1103与迁移往返、Ruff、前端154及lint/build通过。原十题为2完整/6局部/2未验证，复杂问题仍未闭合。详见[39号修复与独立验收报告](39-Phase8任务覆盖修复与独立验收报告.md)。以下保留旧阶段快照；36号根因更正仍有效。STOP，不进入Phase9。

# Phase 8 Research 根因与新对话交接报告

日期：2026-10-17。负责人：三文鱼。角色：Codex Supervisor → 原 Claude Code Worker。

**当前状态：Phase 8 PARTIAL，Research 质量 FAIL，未验收、未提交、未推送。** 最近一次代码全量测试通过，不等于研究质量通过。本轮只做根因分析、离线回放及交接文档归档，没有实施新的产品修复，没有新增真实模型调用；下一轮修复效果与质量仍待验证。Phase 8 后立即 STOP，不进入 Phase 9。

本文是新对话主入口；[35号报告](35-Phase8Research分层定位与本轮复验.md)保留最近实测过程，[32号报告](32-Phase8架构验收与交付报告.md)保留交付历史。旧文件、AGENTS.md、README 中 Phase 2/4/6 的阶段边界或旧测试数字是历史文本；以三文鱼最新 Phase 8 授权、本报告及当前代码实测为准，不因此推进 Phase 9，也不将历史限制改写为 PASS。

## A. 项目概况与已实现范围

项目位置：`/Users/salmon/Desktop/InsightFlow`。产品目标：把原始用户反馈逐步转为可追溯的分类、聚类、Insight/Evidence、机会评分和 PM Decision，进而形成 Requirement 与可人工审核的 PRD；外部 Research 为竞品/公开信息补证据，Tool Layer 在用户批准后执行受控动作。

技术栈：Python/FastAPI、SQLAlchemy、Alembic，正式数据使用 PostgreSQL/pgvector，SQLite 用于部分回归；前端 Next.js 14/React/TypeScript；真实模型接 MiniMax 的 Anthropic-compatible Messages 接口，外部搜索使用 Tavily。

| 范围 | 当前实现与限制 |
| --- | --- |
| Phase 0—1 | Project、CSV/Raw Feedback、分类与可审计 Run 已实现；原文保护、项目隔离不可破坏。 |
| Phase 2—3 | Embedding/Clustering、Insight/Evidence 已实现；真实语义 Embedding、正式人工语义复核等历史限制继续保留。local-hash 不等于真实语义质量。 |
| Phase 4—6 | 机会评分/决策、Requirement/PRD、人审和 Evaluation 有实现及历史工程证据；正式业务评分、完整链路语义质量/标签等缺口以对应阶段报告为准，不能因 Phase 8 工程通过补签。 |
| Phase 7 | 已有前端 MVP、typed API、真实后端接线及项目隔离证据；不是静态原型即验收，历史完整用户链路门槛保留。 |
| Phase 8 Research | `research_runs/sources/findings`、迁移0012、Tavily、版本化 Prompt、严格来源和引文守卫、API及 Insight 页面已接通；当前主要阻断是回答质量。 |
| Phase 8 Tool | 迁移0013、静态工具注册、冻结 Prepare Preview、Cancel/Approve、审计、幂等及并发保护已实现；执行目标只有数据库内 SimulatedIssue。 |
| 真实外部集成 | Jira、Notion、飞书写入和通用 MCP 尚未接入，DEFERRED；`SIM-...` 只能叫模拟结果。 |

Research 已接 `/insights/{insight_id}/research` 启动及历史、`/research/{run_id}` 和 Sources/Findings 查询；请求必须保持项目归属校验。Tool 已接 Requirement 下 Prepare/历史及 ToolRun Cancel/Approve。精确路径和字段以 `backend/app/api/research.py`、`tools.py` 及 OpenAPI 为准。

## B. 当前 Git、文件、环境与保护边界

本次交接写入前现场确认：HEAD `449e879`，分支 `codex/phase6-evaluation`。分支名旧不代表可擅自切换。大量 Phase 8 修改/新文件未提交；包括 Research/Tool 服务、API、模型、迁移0012/0013、前端详情/typed API、测试、文档，以及两份原型 ZIP。不要 reset、clean、checkout 覆盖、自动暂存全部文件或把已有改动当作垃圾。

本轮只新增/更新交接文档和公开诊断证据；没有改产品代码、AGENTS.md、CLAUDE.md、权限、密钥或用户数据库。交接前后文件保护清单由 Codex 比对，结果另存本机 `work/phase8-supervision/20261017-handoff/`。

必须保护：

- 两份根目录用户原型 ZIP、Raw Feedback 和数据来源；不得修改原文或删除证据。
- `.env`、`key/.env` 与进程环境中的密钥；不得输出值、提交或写入报告。`key/.env` 是 JSON 格式配置，不能当 shell 脚本 source。
- 历史迁移和 Phase 1/2/3 Prompt/业务逻辑；Phase 8 改动不能回写旧迁移。新迁移不是默认方案，必须确有持久化需要且经 Codex 评审。
- 用户数据库不能执行 downgrade；破坏性迁移往返仅在全新临时库运行。先读 `verify_postgres.py` 确认隔离方式。
- 同一工作树同时只有一个写入者。Claude 工作时 Codex 只做只读监督；结束后才独立写测试/验收/文档。

上轮环境事实（新对话需现场刷新）：两套用户 PostgreSQL 数据目录 `work/local-postgres`、`work/phase7-user-postgres` 的只读连接报 `global/pg_filenode.map: Operation not permitted`，其实际 revision/行数未测量；未重启、降级或改权限。8000 端口当时属于其他项目 AI小商，未干预。Phase 8 验收另用新建临时 PG 与18080/13080端口，相关临时服务已退出；新对话必须先核对端口归属，不根据旧记录杀进程。

Tavily 曾有 cwd/环境继承差异：后端目录启动进程未读到，根目录启动进程可以读取 `TAVILY_API_KEY`，并已完成真实十条调用。不能再次根据旧缺配置提示认定本机无密钥。新对话只检查字段是否存在；确实缺失时才告知三文鱼期望位置，绝不要求贴密钥。真实未调用前标 NOT MEASURED。

真实验证脚本明确读取进程环境或 `backend/.env` 中的 `TAVILY_API_KEY`；模型需要 `LLM_API_KEY/LLM_BASE_URL/LLM_MODEL`，缺少时可在隔离进程内映射 `key/.env` 的 `ANTHROPIC_AUTH_TOKEN/ANTHROPIC_BASE_URL/ANTHROPIC_MODEL`，不修改该文件。配置存在不等于调用通过。

## C. 最近一次最终代码验证：历史实测，接手后仍需刷新

以下来自35号报告及本机日志，不是本次文档交付重新跑出的数字：

| 检查 | 最近最终代码结果 | 边界 |
| --- | --- | --- |
| Research 专项 | 283 passed | 自动结构/行为回归，不能证明模型回答质量。 |
| 后端 SQLite 全量 | 972 passed / 11 skipped | 跳过项不能算通过。 |
| 全新隔离 PostgreSQL 全量 | 983 passed | 并完成完整迁移 upgrade/check → downgrade base → upgrade/check。 |
| Ruff | PASS | 范围 app/tests/alembic/scripts。 |
| 前端 | 147 passed / 15 files，lint/build PASS | 对应当前 v4 前端；以后修改需重跑。 |
| 真实浏览器 | FastAPI＋生产 Next.js Research/Sources/项目隔离，模拟 Prepare→Cancel→Prepare→Approve/重复批准 | 有API/DB证据；SIM只有本地模拟，真实外部集成未测。 |
| Tavily＋MiniMax-M3 十条 | 程序5 completed / 5 partial_failed | Codex官方原文核对仅2完整回答、3局部事实、5未验证，质量FAIL。 |
| 本次离线根因验证 | 九条可回放结构输出结果全部复现；完成判定、换行表示、数量门等对照已完成 | 无网络模型调用、无数据库写入；不能替代新的真实质量验收。 |

历史正式人工签字、长期语义指标、完整 CSV→AI→PRD 的本轮新建数据链路等尚未测量的项目继续保留；Codex 来源核对不是三文鱼签字。

## D. 已做修复、会话与下一轮职责

上一轮已修复查询保留 manual/manually、add、Mobile，Apple Support 品牌不再盖过 iCloud.com/Shared Photo Library，有限词形避免 viewpoint/shareholder 等误命中；新增独立 `research_agent_v4.py`，保留v1—v3，closed schema仍从v2 deepcopy。前端所有 Research 终态增加“回答完整性：未验证”。服务核心引文/数量/官方守卫、完成门槛、事务与重试预算没有被放松。

| 工作 | 原 threadId | 最近 jobId |
| --- | --- | --- |
| Research Worker | `8d048dd4-fb66-4838-898b-44f10cedd34c` | `46a27c79f3914bb59c69e8efe069c1e2`、`19900ffa7c8b4dcd83d4554eeadd2c8d` |
| 前端 Worker | `42e973e0-5295-496d-a9bd-7c08d6d24ae4` | `dc6b1408ebd745a0863488060f07d2af` |
| Tool 历史修复 | `520a4b77-2518-4ca2-9fc9-439b21245afb` | `705a89ad650944faa83680ad78c07b05`、`73b9235182f04ff49c058f61e571a54d` |

本次根因和交接没有发起新 Claude job。后续失败带具体 Run、原问题、实际 query、source 原文、payload、first failed guard、命令与失败断言，续接对应原 threadId；每个新 jobId 立即保存。Worker 自述不是验收证据；曾发生自述没运行命令但日志有只读命令的情况。具体边界见35号报告。

Codex 负责范围/架构/独立验收/Git。Claude 只实现逐批明确下发的文件，不运行任何 Git、不改 `.git`、AGENTS.md、CLAUDE.md、权限、密钥、历史迁移，不通过脚本绕过限制。沿用 MCP edit tier＋受控 acceptEdits＋simple；不要为执行方便修改权限。上轮后续已禁止 Worker Bash，测试由 Codex 接管；新对话沿用这一保守边界，只授权需要的读写工具。

## E. 十条真实样本：不更换问题美化完成率

原问题、全部轨迹和 Sources 在 [v4真实证据](evaluations/phase8-v4-live-evidence.json)，这里按原顺序索引：

| # | Run ID | 程序状态 / 判题 | 当前明确断点 |
| --- | --- | --- | --- |
| 1 | a8ae9457-180b-4c2a-8c19-cdfb263efdbf | partial_failed / 未验证 | Google Photos多图关键段落未进snippet，several丢失；字面量换行导致引用不匹配，首轮risk refs不变量失败。 |
| 2 | 4d909d70-7542-4585-8b7e-4de5277afc3d | partial_failed / 未验证 | Dropbox多图数量证据不足；overnight句确在所引片段，首先被数量门拦下；格式事实名称含来源无支持的mobile。 |
| 3 | 85216389-f1a1-489d-9d13-1260d8ac01d6 | completed / 局部事实 | OneDrive iOS16手势限制触发完成；缺目标手动多图完整流程。 |
| 4 | 2579bee6-7d90-4cd2-9528-4e4bffb7d8e0 | partial_failed / 未验证 | iCloud入口并列仍只一次搜索；首次缺risks被误称未知顶层字段；后续候选不满足数量/名称门。 |
| 5 | d55a6d3c-662b-4123-911e-89cb5375d510 | partial_failed / 未验证 | Drive普通上传不证明明确多选；模型字面量换行拼接失败。 |
| 6 | 72fd1637-e6d6-4e51-bcdc-97662fcd6c0f | completed / 局部事实 | Google Photos手动步骤引用失败；漫游/自动流量设置触发完成。 |
| 7 | 1e0f9410-3df3-4ebd-8f14-f787d3ec2d52 | completed / 完整窄回答 | Dropbox camera uploads未启用仍可手动添加，有官方原文；不能外推明确多选。 |
| 8 | 551a53d9-b13f-4fe4-b9cb-b2f1fd1e1142 | completed / 完整窄回答 | OneDrive Choose Existing、选择已有图片并Done，有官方原文；额外iOS16说明仅在其范围有效。 |
| 9 | f34d4fc4-fc99-45c7-8b67-2bd285bbdf52 | completed / 局部事实 | iCloud网页上传目标答案未被检索到；查看/加入图库等旁支触发完成；首轮opportunity refs不变量失败。 |
| 10 | 8f1e4a00-45bd-45dc-9c9e-a1f05d2063d0 | partial_failed / 未验证 | Drive所需步骤已有来源，三次缺submit_research工具块；缺安全响应元数据，不能确定更深协议原因。 |

上述 Run 属于已关闭的隔离临时库，不保证能通过在线 API 再查询；冻结证据文件才是复现入口。5/10程序完成不能改写为5/10完整回答；三条completed存在完整性误阳性。

## F. 新对话建议执行流程

### F1. 先审计与复现，再实施

完整阅读 `AGENTS.md`、本文、34/31/32/35/33号文档，再按入口读取 `AGENT_HANDOFF.md`、`技术开发规范与Git规则.md`、`工作记录.md`、`docs/11-长期开发总纲与Agent交接规范.md`、`docs/08-决策记录与修正.md`、`docs/09-验证与交付记录.md`、原Phase8任务与已有人工复测流程。从当前代码核对旧规范的阶段差异，不能根据历史文字删除Phase8实现。

先保存当前Git差异及保护清单，核对实际代码、迁移头、数据库、API、进程和必要基线；不连接不明来源数据库做迁移，不抢其他项目端口。回放冻结证据，确认本文根因仍能复现。

### F2. 将待解决问题转成架构契约和失败样例

先明确内部任务表示、核心问题/辅助事实、各子问题证据绑定、缺口、完整性状态、失败原因与成本上限。不能依赖模型自评完整性，也不能只增加关键词/Prompt禁令。方案仍属Phase8；不引入通用多Agent系统、真实外部写入或Phase9。

保留全部v14守卫，区分：来源真实性、引用精确性、事实相关性、整题覆盖、工程执行状态。明确旧API/前端兼容方式；如确需schema/API或持久化变更，先由Codex评审范围再下发，保留版本，不改旧迁移/旧Prompt。不要默认把“加一个答案字段”当成已解决覆盖验证。

将本轮反事实变成独立行为回归：漫游开关不得完成手动步骤；仅一步不得完成多步问题；iOS16限制不得完成跨版本多选流程；缺来源不能推断不支持；官方正文存在而snippet缺失应报告证据缺口；字面量转义不能误变成原文；not_verified的refs不变量要给正确反馈。

### F3. 原Claude会话分批最小实现，单写入者

建议顺序（属于待设计/待验证方向，不是已经实现的功能）：

1. 任务/证据覆盖与完成门槛；正确区分局部事实、未验证和整题完成。
2. 在现有官方边界内按缺失子问题补检索/读正文；修正搜索命名空间不一致，保留快照及原文定位。官方正文读取必须保留域名/重定向校验、超时和内容上限，避免新取证路径绕过来源守卫。
3. 模型选择证据ID/定位，由代码从保存原文构造引用并再校验；禁止盲目反转义/宽松空白匹配让模型文字直接放行。
4. 针对软降级提供安全、具体、有界纠错；区分缺资料、格式错误和协议异常；补安全元数据，不能保存密钥/Raw Feedback。
5. 只有后端契约稳定后才按需续接原前端会话；任何时候只允许一个写入者。

每批写清允许文件、禁止文件、预期行为、验收样例和命令。失败以具体证据续接同threadId，保存jobId。Codex独立审查实际diff，不能把Worker汇报当完成。

### F4. 最终代码完整回归与真实质量验收

最终修复完成后执行，避免把中间版本通过报告挪给最终版本：

```bash
# 在 backend 目录：先确认测试只用隔离数据
uv run pytest -q
uv run --with pgserver==0.1.4 python scripts/verify_postgres.py
uv run ruff check app tests alembic scripts

# 在 frontend 目录
npm run lint
npm test -- --reporter=dot
npm run build
```

PostgreSQL必须全新隔离、全量测试、完整迁移 `upgrade/check → downgrade base → upgrade/check`，确认相应成功标记；禁止对用户库降级。修复新增迁移时必须纳入完整链。

随后在自己新建临时PG、真实FastAPI＋Next.js生产构建中，用浏览器实测Research/来源/完整性与项目切换；测试模拟Prepare→Cancel→Prepare→Approve、重复批准同ID、仅一条模拟目标、跨项目404、撤销批准不执行，并按33号流程检查Raw快照。真实外部Jira/Notion/飞书/MCP继续DEFERRED，不在本轮偷偷接入。

真实调用从项目根目录启动：

```bash
uv run --project backend --with pgserver==0.1.4 python backend/scripts/verify_phase8_research_live.py --check-config
uv run --project backend --with pgserver==0.1.4 python backend/scripts/verify_phase8_research_live.py --samples 10
```

check-config只证明字段存在。完整记录原题→实际query→过滤前后来源→每次结构输出或安全协议元数据→每个守卫/重试→最终发现/覆盖判定。保持同一题集，5—10条逐条核对官方原文，分别标完整回答、来源支持局部事实、未验证、事实误阳性与完整性误阳性；不得换成简单题提高完成率。

### F5. 交付Gate

工程全量、真实Research质量、审批/隔离及文档门槛全部满足，才由Codex核对暂存范围后本地提交；禁止push。质量仍失败就如实PARTIAL、不提交，保存失败Run、原文与复测方法。未测项目明确NOT MEASURED，不用配置存在、Mock、诊断人工payload或模型自评分顶替。关闭自己创建的临时服务，不触碰用户服务。Phase8结束立即STOP。

## G. 证据索引与关键文件

| 资料 | 位置与用途 |
| --- | --- |
| 最近真实十条完整轨迹 | `docs/evaluations/phase8-v4-live-evidence.json` |
| 旧v14来源证据 | `docs/evaluations/phase8-v14-layered-evidence.json`；缺原始模型轨迹，不能装作完整恢复。 |
| 本轮离线诊断归档 | `docs/evaluations/phase8-rootcause/`：replay/controls JSON、两份脚本、官方页面复核元数据、SHA-256 manifest。 |
| 原始实测JSON | `work/phase8-supervision/20261017T010414Z/live-research.json`；本机忽略目录。 |
| 全量日志/截图/保护审计 | `work/phase8-supervision/20261017/`：phase8-final-*.log、jobs、final-audit、protection-audit、浏览器API/DB证据等。 |
| 原任务、范围与人工流程 | 31、32、33、34号文档与长期总纲；历史结论按时间区分。 |
| 给新对话的Prompt | [37号启动Prompt](37-Phase8新对话启动Prompt.md) |

核心实现：`backend/app/services/research_service.py`（查询/过滤/校验/完成门槛/执行闭环）、`research_provider.py`（一次搜索及摘录）、`research_client.py`（tool_use协议）、`backend/app/agents/prompts/research_agent_v4.py`、`research_agent_v2.py`（继承schema）、`backend/tests/test_phase8_research.py`、`test_phase8_research_v4.py`、`backend/scripts/verify_phase8_research_live.py`。前端为 `frontend/src/app/insights/[id]/page.tsx` 及测试、typed API；Tool服务/测试已有通过证据，非Research修复默认范围。

以下为本轮根因详情与对照实验；所有修复建议均未实施。

---

## 结论

主因是任务契约和成功判定没有对齐：产品接收具体 Research 问题，但执行链仍然是“一次搜索 → 生成竞品/市场发现 → 至少有一条合法引用即完成”。系统验证某个片段是否真实，却没有记录和验收整题所需证据是否齐全。检索遗漏、模型引文表达错误以及无纠错反馈叠加，导致既有失败，也有合法事实触发整题完成的误阳性。

不能将问题简单归因为“MiniMax 不行”“官方无资料”或“引文校验太严格”。本轮没有放宽任何生产守卫。

## 方法与证据边界

输入为当前代码及 `docs/evaluations/phase8-v4-live-evidence.json` 中保存的十条真实 Tavily + MiniMax-M3 轨迹。未重新运行十条真实模型调用。

- 对九条包含原始结构 payload 的最终输出，启用生产路径全部 name / quote / description / quantifier 守卫回放；九条 findings 的 category/title/description/status 序列与当时捕获的 validator 输出全部一致，九条完成门槛也全部吻合。
- 第十条缺少原始模型响应，不能重建内容，只能确认留存的三次协议失败及错误信息。
- 对官方 Google Photos、Google Drive 页面重新直接读取；对 Apple 官方手册做只读搜索、读取与 URL/校验器对照。搜索工具的新结果不算项目 Tavily 调用，也不算新一轮 Research 质量验收。
- 单变量实验使用人工构造的诊断输入，只调用当前纯校验函数，不写数据库，不能计为真实模型成功。

离线脚本：`docs/evaluations/phase8-rootcause/replay.py`、`docs/evaluations/phase8-rootcause/controls.py`。
离线结果：`docs/evaluations/phase8-rootcause/replay.json`、`docs/evaluations/phase8-rootcause/controls.json`。
官方网页复核元数据：`docs/evaluations/phase8-rootcause/official-pages.json`、`docs/evaluations/phase8-rootcause/apple-paths.json`。

## 1. 首要根因：事实存在性被用作问题完成性

位置：`backend/app/services/research_service.py:319`、`:3908`。

`_has_external_confirmation` 只要求某个 competitor / market_pattern 为 confirmed。它不接收研究问题，不核对平台、入口、操作、步骤或子问题覆盖。

模型输出只有六个顶层字段：summary、confidence、competitors、market_patterns、opportunities、risks。没有问题拆分、各子问题的答案、覆盖状态与证据绑定。summary 还明确只能依据内部 context，不能承担外部研究答案。Prompt 虽要求不要混淆平台/流程，但服务端并没有通用的平台/操作相关性判定。

反事实实验：以第六题的真实来源作为唯一证据，问题始终为 Google Photos Android 单张手动备份步骤：

| 仅替换候选事实 | 全部严格守卫 | 完成门槛 |
| --- | --- | --- |
| 官方单张备份中的一个正确步骤 | confirmed | true |
| 官方漫游备份开关 | confirmed | true |
| 正确步骤前增加无证据否定 | not_verified | false |

因此即便只给一个步骤，也能完成多步问题；旁支自动设置也能完成手动步骤问题。守卫能拒绝引文外扩写，不等于能判断引用回答了什么。

三条实际误阳性：

- `85216389-f1a1-489d-9d13-1260d8ac01d6`：iOS16 双指手势限制触发完成，缺目标多图手动上传流程。
- `72fd1637-e6d6-4e51-bcdc-97662fcd6c0f`：漫游开关与自动备份流量限制触发完成，目标手动步骤未通过。
- `f34d4fc4-fc99-45c7-8b67-2bd285bbdf52`：查看/加入图库、相机分享等局部事实触发完成，缺网页上传选项和 Shared Library 上传位置。

这首先是架构与验收契约问题，不能只归责于模型没有遵守 Prompt。

## 2. 检索只保证来源入口，不能保证拿到答案段落

位置：`research_provider.py:315`、`research_service.py:1509`。

当前只有一次逻辑搜索，最多五条结果；`include_raw_content=False`。传输重试并不会产生新的问题分解或证据查询。没有读取官方全文、围绕缺失子问题补搜、按各入口保留证据配额的闭环。

### Google Photos：正确页面，错误摘录覆盖

Run `a8ae9457-180b-4c2a-8c19-cdfb263efdbf`。

原问题有 several，实际 query 没有数量词。保存的核心 snippet 长 2184 字符，Tips 后是省略符，跳过多图操作，转去别的章节。重新读取官方全文确认 Tips 中确实说明多图长按选择后备份。

这证明“目标段落没有送给模型”；不是 4000 字符本地截断，也不是官方没有资料。不能仅凭此次记录证明补回 several 就一定提高 Tavily 命中率，该效果仍需同一 provider 的对照实验。

来源：https://support.google.com/photos/answer/6193313?hl=en&co=GENIE.Platform%3DAndroid

### iCloud：搜索命名空间比官方校验允许的范围窄

两个 iCloud Run 的实际 query 都限定 `site:support.apple.com/en-us`，但官方规则还允许 `/guide/` 和 `/icloud/`。

重新找到能回答第九题的官方手册，正式地址为：
https://support.apple.com/guide/icloud/upload-and-download-photos-mmb5dbaf376f/icloud

该页明确给出手机/平板的三个上传入口，并说明 Shared Photo Library 应使用设备上的 Photos App。该 URL 通过现有官方 host/path 校验，但不在实际查询的 `/en-us` 路径之下；`/en-us/guide/...` 别名也重定向至该正式地址。

这是确定的查询范围与允许范围错位。未执行同一 Tavily 的仅路径变更 A/B，不能量化它对排名的影响，也不能称它是所有 iCloud 失败的唯一原因。没有修改官方 allowlist。

补充：搜索找到的部分其他 locale 官方手册不在现行 allowlist 内，分析中未将它们加入生产来源或用于提高完成率。

## 3. 模型协议和证据表达契约脆弱，而且存在文档错位

位置：`research_service.py:2055`、`:2139`、`:2288`；`research_client.py:188`。

对九条最终结构输出中的 30 条 competitor / market_pattern 候选，按生产守卫顺序统计第一阻断点：

| 结果 | 条数 |
| --- | ---: |
| 引文匹配失败 | 8 |
| 数量条件不满足 | 7 |
| 名称未受到引用来源支持 | 5 |
| 全部通过 | 10 |

这些是候选 finding 条数，不是 Run 成功率。各守卫可能同时失败，表只计算最先失败者。

八条引文失败中，六条含字面量反斜线+n；另两条涉及改写、删并原文等字面不一致。部分候选还同时超过引文解析器的 200 字符上限，或包含内部双引号，不能将这八条都等同于事实不存在。

单变量实验使用官方 snippet 原本就有的四步单张手动操作，保持问题、来源、全部守卫和引文内容不变：

| 引文表示 | 长度 | 校验结果 |
| --- | ---: | --- |
| 以字面量 `\n` 连接 | 147 | not_verified |
| 改用来源中的真实换行 | 144 | confirmed |
| 只保留一个原文步骤 | 19 | confirmed |

没有修改 validator，也没有引入空白归一化。该结果证实表示层错误可以独立造成失败。它同时暴露 Prompt 与代码描述不一致：Prompt 声称多行引文被拒绝，实际 regex 可以接收真实换行；引文存在隐含的 200 字符上限，tool schema 的 description 却允许到 4000 字符。Prompt 还提及显式数量，但当前数量 regex 对 `two`、`3` 这样的表达不识别。这些错位应先统一契约，不能靠放宽既有守卫修复。

另外，四条抽象 market_pattern 名称被名称门拦下；Dropbox 的格式事实引文存在，但其来源不含标题额外加入的 mobile。这说明生成抽象结论/展示名称与严格词面名称支持也有摩擦，不能把这些都归成检索不足。

第十题 Run `8f1e4a00-45bd-45dc-9c9e-a1f05d2063d0` 的普通 Google Drive 步骤已在检索片段中；三次失败都发生在工具输出协议层。当前只保存错误类型/安全消息，没有原始响应的 stop_reason、block 类型与 usage。诊断证明 end_turn、max_tokens 等不同安全元数据在缺 tool_use 时产生相同错误，故不能进一步断言是模型能力、输出截断、兼容层还是拒绝调用工具。

## 4. 纠错闭环断在软降级与错误提示

位置：`research_service.py:3740`、`:3854`。

只有抛出的结构/协议异常进入重试。name/quote/description/quantifier 守卫的失败通常返回 not_verified，而不是抛异常；因此 payload 一旦结构合法，就跳出 LLM 循环，然后才检查有没有确认事实。

第2、第5题均只用一次模型输出就结束；第1、第4题修正结构后，全部事实降级仍立即结束。系统没有把“哪一条因哪一门失败、还缺哪个子问题”送回纠错，也没有补检索。不能仅增加重试次数；重复同一片段无法补回缺失的关键证据。

错误提示还存在失配：

- 第1题首轮实际错误是 risk 标成 not_verified 却带 source_refs；第9题首轮是 opportunity 的同类错误。它们被归为 source_refs_invalid，提示却主要要求 source_refs 包含合法字段和 ID，没有指出未验证项必须清空 refs。
- 第4题初轮缺少 risks 字段，实际异常文本是 unexpected top-level fields: []；不是多了未知字段。分类和提示没有区分缺失字段与额外字段。

## 5. 数量守卫应保留，但不能代替问题结构

多选问题会将“引用中必须含数量词”施加到每一条 competitor / market_pattern。于是合法的普通步骤、自动上传区别、限制等辅助事实也可能降级。相反，一条带 multiple 的 iOS16 手势限制可以通过，但仍无法回答目标上传流程。

同一官方单张步骤，在单张问题下 confirmed，在多选问题下 not_verified；单变量实验复现。该降级正确阻止用普通步骤证明多选，不能为完成率去掉它。缺口在于没有分开表示“核心多选能力证据”和“真实但不足以回答核心问题的辅助事实”，也没有整题覆盖门槛。

## 6. 自动测试没有覆盖真实质量分布

`test_phase8_research_v4.py` 的16个测试中，10个检查 Prompt 文本、版本或 schema 契约，6个执行查询/校验行为。旧 `_install_llm_stub` 还会自动补入有效引文，适合隔离测试其他路径，却不能测出真实模型的转义、漏字段或协议失败分布。

一些查询测试还固定要求采用第一个 path_prefix（Apple 为 /en-us），没有测“同一允许列表里的官方手册能否被检索到”。

这些测试和完整工程测试有价值，证明实现按现有规则运转；它们不能证明这些规则足以完成研究问题。现有真实审计脚本明确不自动签发语义 PASS，这一点应保留。

## 需要更正的上轮归因

对冻结原始 payload/source 的本轮回放优先于旧报告文字：

1. 第2题 overnight 句实际存在于其所引的 `97645b-00` snippet 中。先前报告写“引错来源/片段没有该句”不准确；该条最先被数量门拦下。
2. 第4题所谓 unknown_top_level_field 实际是缺少 risks；它不是多出某个未知字段。
3. “跨行引文被拒绝”过于笼统：本轮单变量实验区分了字面量转义错误、长度限制、内部引号与真实换行。真实换行在现有实现中可以通过。

根因分析时保持仓库只读；随后经三文鱼明确要求编写交接报告，本次将诊断归档到项目并在旧报告增加更正入口，保留原有历史文字。

## 修复优先级与边界

1. 先建立可检查的研究任务与证据覆盖契约：把产品/平台/入口/操作/数量/限制分开；局部事实、证据不足、整题完成必须分别表示。不能以模型自评“答完”作为验收。
2. 在现有官方来源边界内补足检索闭环：按子问题检索，必要时读取官方正文相关段落；查询覆盖允许的官方手册命名空间；保留内容快照与证据定位。不因检索不到就推断不支持。
3. 由确定性代码从保存的原文生成引用展示。模型选择证据 ID/片段定位；代码重新提取准确原文并执行全部旧守卫，避免让模型负责手工复制转义字符串。禁止对模型文本盲目反转义后直接放行。
4. 为软降级提供安全、具体的诊断与有界纠错；缺资料走补检索，格式错误走结构纠错；分别报告 missing/extra fields 与 status/refs 不变量。重试保持成本上限。
5. 补安全响应元数据和冻结真实样本回归：明确拒绝“漫游开关完成手动步骤”等案例。之后再用真实调用及官方原文验收质量，不能拿人工构造的诊断成功代替。

这需要调整 Research 的契约与闭环，继续增加 Prompt 禁令或替换几个查询词不足以解决主因。无需据此进入 Phase 9、引入重型 Agent 编排或先行改动历史迁移。

## 根因分析时的判定

沿用留存的真实十条证据：2条完整回答、3条局部事实、5条未验证；程序状态5 completed / 5 partial_failed。当前质量 FAIL、Phase 8 PARTIAL，未验收、未提交、未推送。本轮只做根因分析；新的修复效果及新的真实质量评估均未测量。


## H. 离线诊断重放方法

从项目根目录执行以下脚本。它们只读取冻结的公开来源/模型输出并调用纯校验函数，不连接数据库、不调用搜索或模型、不修改产品代码；导出的新结果在`/tmp`，不会覆盖已归档JSON。

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python docs/evaluations/phase8-rootcause/replay.py
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python docs/evaluations/phase8-rootcause/controls.py
```

预期：九条最终结构输出完成门槛吻合；controls中`accepted_payload_replays_exact_match`为9/9。字面量换行为false、真实换行为true；漫游开关对手动问题仍被原完成门槛接收；无证据否定被拒绝；Apple官方guide URL通过现有allowlist但不在搜索的/en-us路径内。实现变更后这些结果可能变化，须明确记录新旧差异，不能为了重现旧结果恢复缺陷。

注意：这是诊断脚本，不是新的正式业务测试套件；controls的原始样本匹配统计不包含完整source_refs序列比较，不能扩大宣称为所有字段逐字一致。不能把人工构造的confirmed计入真实模型质量分母。

# Phase8 当前进展、痛点、解决方案与新对话交接

交接时间：2026-10-18 16:48（北京时间附近，以本机证据时间为准）。项目：`/Users/salmon/Desktop/InsightFlow`。负责人：三文鱼；执行：Codex。

**当前结论：Phase8 PARTIAL，Research 质量门槛未通过，正式用户验收 NOT MEASURED。未暂存、未提交、未 push，不进入 Phase9。** 本文是工作交接，不是最终交付报告。用户此次要求整理文档并转新对话继续，因此本轮在文档交接后结束；不是阶段完成。

## 1. 最重要的最新指令

三文鱼最新明确要求：

> 接下来的任务不要再让 Claude Code 来改代码了，希望 Codex 亲手修改，快速完成本阶段。

该授权覆盖 AGENTS.md、44/45/46号中此前要求由 Claude 写入、沿原 Claude threadId 修复的执行方式。**下一对话由 Codex 亲手修改源码、测试和文档，不再派 Claude 写代码。** 单工作树单写入者、保护范围、预算、独立验收和 Git 门槛继续有效。旧 Worker 的退出、超时或空响应不算代码实现；历史账本保留，不要续派。

用户已授权继续完成 Phase8，无需再次询问“是否继续”。仍需先现场核对和根因复现，再做有明确范围的修复；不要重新从头设计已经完成的45号计划，也不要直接堆补丁。不得代签三文鱼正式验收。

## 2. 阅读顺序与当前现场

新对话先读本文和48号续接Prompt，再读 AGENTS.md、45号设计及46号进度。43/44号是根因及约束来源，42号是 v6 历史验收，不是最新代码成绩；技术开发规范、工作记录、长期总纲、V1 PRD/修订、原 Phase8 任务、人工复测流程仍按43号索引读取。入口里的旧“尚未实施v7／继续Claude”只代表历史快照。

现场实查：

| 项目 | 状态 |
|---|---|
| HEAD | `449e8790b8da8b9f04f4e3739cd9740606241cc4` |
| 分支 | `codex/phase6-evaluation`，沿用既有分支，不因名称切换或清理 |
| 暂存区 | 空 |
| 工作树 | 大量已有 tracked 修改和 untracked Phase8 文件，必须保留 |
| 临时验收 PG | 本轮自建隔离库，现场 revision `0015` |
| 用户数据库 | 本轮未操作、未改权限、未 downgrade、未重启恢复 |
| Claude | 本轮无活跃写入任务；机器还有其他 Claude 进程，不可据名称擅停 |

现场摘要、六个最新源码/测试指纹、保护审计和隔离库备份信息：

`work/phase8-supervision/20261018-handoff-v7/handoff-state.json`

本次从设计基线选取80项旧测试、历史Prompt、迁移、AGENTS、ZIP及密钥等保护项，**0项漂移**。这是本次明确筛选的80项，不冒充此前89项清单的重新验证。原基线：`work/phase8-supervision/20261017-design-v7/baseline-file-manifest.json`。最终交付仍须按完整保护范围重新核对。

## 3. 已实现的业务机制与边界

目标仍是 PRD 与 Phase8 要求中的竞品能力、设计/限制/差异、样本模式、场景优缺点、机会风险，以及有依据的 Requirement/PRD。不能把完成数、引文数、页面区域或测试通过当业务完成。

### 3.1 Research v7 已落盘

主要文件位于 `backend/app/services/`：

| 职责 | 实现文件与当前行为 |
|---|---|
| 原问义务 | `research_input_v7.py` 保留动作、对象、产品、模式、设备、数量及原文span/归属；不由模型定义自己的验收分母 |
| 规划契约 | `research_spec_v7.py` 闭式字段、政策与业务条件分离、比较/版本/分析可表达；未知或未覆盖义务保留缺口 |
| 精确事实 | `research_catalog_v7.py`、`research_support_v7.py`；同Run/项目、原快照offset/length/sha256、ID/URL、逐字引用；普通步骤的真实性不因缺多选断言被降级 |
| 支持关系 | 另检设备/动作/对象、模式、数量、条件、版本及完整同流程；有真实引用不自动等于回答原问 |
| 流程身份 | 快照、section、编号列表派生 flow_id/flow_step_total；跨流程拼接、缺步骤/长句片段不能通过 |
| 跨轮保留 | 按要求保留合法支持；反证使前提及依赖比较/分析失效，不能按 confirmed 数量选回较差轮次 |
| 分析与回答 | `research_answer_v7.py` 明确事实、有限推断、待验证建议；模型只提候选，服务器验证支持关系 |
| 有界编排 | `research_pipeline_v7.py` 统一候选ID/attempt/slot、完整结构闭包、原问义务/flow目录、逐轮重验、raw分析候选≤24/交付≤8、事实与分析交付引用并集≤40 |

Codex接管后已亲手修复：40条旧支持+1反证预算回滚保留失效前提；比较父项未随侧项撤销；代表选择突破8条；拒绝诊断全部index0和缺采用来源；后置设备被分给另一动作；宽mobile scope误判Android；同动作不同模式省略宾语；HOW误扩到仅需区分的自动模式；普通数量/动作短语被当业务条件；合法原问辅助导航候选导致整个规划失败。

辅助候选只能在原闭式schema、唯一ID、query引用均合法后局部归位；不能删除原问分母。真条件、真平台冲突及未知业务动作仍须保守处理。

### 3.2 Requirement/PRD 已增加有依据的材料化

新增 `artifact_grounding_v2.py`，接入 `requirement_prd.py` 及新 Prompt `requirement_generation_v2.py` / `prd_generation_v4.py`：

- 原候选先parse并检查商业承诺，再做Raw ID/正文/用户映射冲突、known风险逐字支持及事实材料化，最后校验旧wire schema全部长度/列表预算。
- 问题和背景仅使用对应章节实际引用子集的完整Raw，显示用户报告边界；反馈数、已知独立用户数、未知身份反馈数分开，不拿整个bundle补引用。
- 全局引用索引取实际章节/FR/edge/AC/风险/指标使用集合；不自动塞入所有Evidence。
- 方案、人群、功能、流程标提案；AC标未执行，不能把可验证条件宣称成已通过测试。
- 商业承诺扫描只凭服务器完整字段和精确Raw span proof遮蔽扫描副本里的原Raw；不能凭“用户报告”前缀或其他字段收益愿望豁免模型承诺。
- 超限不截断Raw，旧 AI v1/v3、PM与历史默认契约保持兼容。

新增材料化/API专项42项通过；**真实 v2/v4 新链内容质量尚未最终复验**。仍应补 proof 字段改字、span错位、模型收益承诺跨字段借豁免的最小篡改反例。

### 3.3 项目隔离与前端已接线

`backend/app/api/requirement_prd.py` 的8个详情/写入/历史相关路由支持可选 project_id：传入时在读取、模型调用、版本递增/写入前检查归属，错误项目404；省略保持旧契约。

前端 `client.ts` 及 Insight/Requirement/PRD 正式详情页传入项目；requestScopeRef以项目+路由身份丢弃迟到详情/历史响应，不只依赖abort；写前复核资源项目；新v4 PRD再生成继续明确请求v4，旧版本保留旧行为。Research未知gap保守中文显示。

前端全量182项、lint、生产build曾通过；新项目隔离/延迟响应/v4重生成专项均包含在内。最终仍要用真实页面和API证明。

## 4. 最后一次代码修复：已通过专项，尚非最终验收

### 4.1 局部限制误伤整体极性

第一轮 original #8，Run `42046eca-451e-4b28-8fac-2838f0b1d028`：完整10条流程组包含 `OneDrive cannot upload photos optimized for iCloud.`，被全局标negative；后轮选择同组4条正向分支标positive，账本生成假冲突。

已亲手在 `research_support_v7.py` 增加 `_negative_scope`：对目标对象后的有限英语关系/分词限定区分“子集限制”和目标否定；原问明确请求该限定时保留negative。仅有限制句、没有独立正向支持时为unknown/partial，不反过来证明一般能力。限制引文不删除；一般否定与正向流程仍冲突。

新增正反例：完整一般流程+限定Note；仅子集否定不能证明一般上传；一般cannot/does not support与正向流程冲突；问到限定子集仍negative。该机制是有界句法识别，**不是开放语义证明**，中文/复杂限定及同句混合断言仍须独立反例审查。

### 4.2 产品与设备被做成笛卡尔积

第一轮 extended #6，Run `da890b31-c958-4fab-b5cc-e9fa76525e3c`：用户只问 Google Photos Android 和 Dropbox mobile，义务却有四格，包括Dropbox Android、Google Photos mobile。

已亲手在 `research_input_v7.py` 按平台atom原始owners过滤义务展开：共享动作仍可分别产生两款产品要求；各平台只属于原文声明的产品分支；真正共享设备后缀仍可覆盖并列产品。新测试要求只保留两格。

两个机制修复后，八核心文件**最新230项通过**，日志：

`work/phase8-supervision/20261017-implement-v7/codex-handoff-core-230.log`

交接时四个最新改动文件 Ruff check 通过；`ruff format --check` **失败，尚有2个文件待格式化**：`research_support_v7.py`、`test_phase8_support_v7_supervisor.py`。此轮转为文档交接，没有顺手格式化生产代码。下一对话须处理后重新验证，不能报告格式全绿。

## 5. 新鲜验证成绩与适用范围

| 门槛 | 最近实测 | 当前可作何种结论 |
|---|---|---|
| 八核心Research | 最新代码230 passed | 最新专项通过；非全量/真实质量 |
| 材料化+项目API | 42 passed | 接管实现后的专项，不含最后Research修复 |
| SQLite全量 | 1464 passed / 11 skipped | 早于最后输入/否定/owners修复，最终要重跑 |
| 全新临时PG全量 | 1475 passed | 同上；不替代用户库验收 |
| 另一临时PG迁移往返 | upgrade/check→downgrade base→upgrade/check通过 | 非最新代码最终门槛，旧迁移不改 |
| Ruff全app/tests | 先前通过；最新四文件check通过 | 全量及format须重新完成；已知2文件format红 |
| 前端 | 182 passed，lint/build通过 | 生产浏览器新完整链路未最终完成 |
| 真实Research | 两轮原10+另8均有完整运行文件 | 多项失败，无最终质量PASS |
| 官方原文 | 部分冻结证据已独立定位 | 本轮完整逐题重新抓取/语义验收未完成 |
| Raw | 交接时12行9字段逐字段与原快照相同 | 中间保护审计，不替代最终完整链路后审计 |
| 用户正式验收 | NOT MEASURED | 不代签 |

工程日志在 `work/phase8-supervision/20261017-implement-v7/codex-{sqlite-full,pg-full,pg-migrations,pg-launch,artifact-after,live-contract-after}.log`。

前端三份日志已从易失的/tmp复制至 `work/phase8-supervision/20261018-handoff-v7/phase8-codex-frontend-{full,lint,build}.log`。

验证中遇到的环境问题不要误记产品缺陷：第一次空PG未先迁移，缺vector类型造成420项setup error；修验收脚本先upgrade后，1475项通过。部分新增测试曾有重复参数、默认files误用于photos、引用工厂只带F001等夹具错误，已先纠正夹具再验证产品红例；不得用它们虚增产品修复数。

## 6. 两轮真实题集：分母、失败和已有证据

文件均在 `work/phase8-supervision/`，下表目录内文件名为 `live-research.json`：

| 批次/目录 | 分母 | 程序状态 | provider_failure事件 | 零来源Run |
|---|---:|---|---:|---:|
| `20261018T082746Z-research-agent-v7-original` | 10 | 2 completed / 8 partial_failed | 0 | 2 |
| `20261018T082746Z-research-agent-v7-extended` | 8 | 0 completed / 8 partial_failed | 0 | 0 |
| `20261018T083602Z-research-agent-v7-original` | 10 | 0 completed / 10 partial_failed | 9 | 6 |
| `20261018T083603Z-research-agent-v7-extended` | 8 | 0 completed / 8 partial_failed | 9 | 5 |

事件数不等于失败题数。第一轮在最后输入修复前；第二轮在最后否定/owners修复前。**两轮不是同最终代码稳定性复验，不能直接把下降归因为代码回归或全部归因为搜索。** 第二轮也同时存在协议/规划失败。

原十题不能换分母；中文/改写/分析另八题另列。当前没有新一轮完整/局部/未验证的最终独立语义分数；文件内 human_source_review/semantic_review 仍未测。completed不是质量PASS。

已核对的真实故障：

- original #2 `55adb7ad…`：Dropbox upload-mobile正文已读，存在条件+8步；后轮累计提供77<100，目标流程却仅提供第1步，2–8步/前提未入该轮目录，混入overnight/audio/community候选。**已读正文→完整候选支持组仍有断点，具体过滤位置待冻结回放定位**。不能称供应商没正文。正文普通复数 `Tap the photos…` 仍不能独立证明“多选一次上传”。
- extended #2 `5aa76578…`：完整条件+8步已提供，r1已covered；r2却混绑手动流程和多套相机开关流程，漏所属条件。condition/procedure拒绝正确；要修选证/纠错，不能松守卫。
- original #9 `beefa94c…` 本轮已completed，已交付手机/平板三选项、Shared Photo Library入口与版本。不能继续把该Run写成“第三轮答案被回退”；那是43号的旧Run根因。
- extended #3 `610e166f…`：三轮契约未冻结，出现version="unspecified"、非业务动作/产品字段混用；虽有Shared Library版本原文，另一篇iCloud Drive文件上传不能证明Photos上传入口。
- extended #6 `da890b31…`：四事实和七facet曾covered，但平台义务分母污染、scope误冲突；步骤差额仅是假设，不能证明更省时/体验更好。最新owners与scope修复尚待真实复验。
- extended #7 `141f822d…`：前轮缺market_pattern或挂comparison，第三轮query ID不指requirement；同Run只有Dropbox正文，不能强行生成两产品分析。
- extended #8 `2b80f98f…`：普通备份步骤+Good internet connection不能证明离线可备份，condition缺口应保留。

原始 audit 只看结构化raw_structured_payload、evidence_catalog及安全metadata；不要解析thinking或从任意文本挖JSON。

## 7. 当前痛点与搜索替代方案

### 7.1 已证实与尚未证实的原因

1. **搜索入口不稳定。** 第二轮两套各9次provider_failure，但 `research_pipeline_v7.fetch` 将TransportError/ResponseError统一丢成该代码，丢失安全的HTTP分类/是否可重试信息。因此目前不能断言额度耗尽、认证失效或网络故障。
2. **独立探针不等于服务原环境。** 本轮临时shell探针经正确Settings入口、按脚本方式读取backend/.env后，返回TAVILY_API_KEY未配置；没有发出成功的诊断搜索。它只证明该探针环境缺配置，不能倒推先前真实Run为何失败，也不能认定用户根本没配置密钥。
3. **正文到完整支持组断裂。** 搜索成功也会因候选提供/flow选择/混绑而不能回答；换引擎不会自动修复。
4. **模型闭式协议仍不稳定。** products/action/version/analysis_facets/query IDs出错，占用总LLM3次纠错空间；现有局部归位不能洗白任意非法schema或删除原问要求。
5. **质量验收没有闭环。** 新材料化机制、项目页面和真实AI链还要用最新代码从头走完，不能复用旧产物宣称通过。

### 7.2 可选方案比较

| 方案 | 价值 | 当前证据/限制 |
|---|---|---|
| 修Tavily配置与安全诊断 | 改动最小，保留原接入 | 先区分配置、认证/额度、限流、网络、上游5xx、格式错误；不能泄露响应正文/密钥 |
| 可替换的独立HTTP搜索适配器 | 降低单一供应商依赖，推荐作为正式方向 | 沿用ProviderSource/ProviderSearchResult边界；选择一个有可用凭据和实际可调用接口的后备，先做小规模真实冒烟 |
| MiniMax搜索 | 本轮Codex的MiniMax web_search工具实际返回微软官方上传页 | **只证明Codex工具可用**；后端独立HTTP接口、授权、部署配置、收费/限额尚未验证，当前未接入产品 |
| 原问直接附官方URL | 用户已给链接时可绕过搜索发现步骤，继续正文核验 | 当前是建议，未实现新的URL输入契约；不能把固定十题答案/URL表放入生产逻辑 |
| 已读官方文档中受控发现链接 | 已有入口时可发现关联帮助页 | 仍需允许域/路径、逐跳安全、同Run快照及预算；不是无限爬虫，尚未实现 |

推荐顺序：先安全诊断→核对当前服务配置加载路径→一个独立后备适配器真实冒烟→同一证据管线回归。检索服务只发现URL/摘要，**摘要不升级为事实**；仍独立抓官方正文、校验相关性与支持、输出精确引用。短期可用Codex工具辅助人工官方原文核对，但不能冒充产品已经端到端可用，不能手工注入其他Run搜索结果换绿。

建议新增配置选择provider，保持旧Tavily默认/旧Prompt兼容。失败切换也受同一Run总搜索预算，不能每个provider重置3次；切到另一provider的额外搜索占剩余额度。原有网络重试只按既有有界配置，并单独记录物理尝试数/耗时。先设计记账，再实现，不能“自动兜底”暗增调用。

建议最小代码范围：`research_provider.py`（结构化安全错误/适配边界），必要时新增一个provider适配模块；`research_pipeline_v7.py`（配置选择、错误分类、同Run预算/审计）；`app/db/config.py`（可选配置）；新增supervisor测试及适配器说明。只有确有需要才扩接口/前端，不默认迁移，不更改密钥或受保护旧Prompt。

这些方案**尚未实施**，用户已询问可替换性并要求本次交接，下一对话从安全诊断与可调用性验证开始。

## 8. 真实浏览器、临时环境和链路现场

本轮自建隔离环境仍保留运行，交接时：API `127.0.0.1:18080` PID60880；生产Next `127.0.0.1:13080` PID60881；监督launcher PID35393。PID和端口会过期，先重新核对归属，不可按历史PID杀进程。其他项目/用户PG服务不动。

launcher源：`/tmp/phase8_v7_e2e_server.py`，已复制到本地交接证据目录。控制目录：`work/phase8-supervision/20261018-phase8-v7-e2e/`。

- `restart-api` / `restart-next`：仅用于自己拥有的服务；最新Research修复尚未装载到旧API，最终需更新后再验。
- `server-stop`：会终止这套自建服务并清理其临时PG；**继续使用当前链路前不要创建它**。
- `isolated-db-uri.txt`：只供本机程序读取，不打印、不提交。
- `/tmp`脚本副本、前端日志、库备份位于 `work/phase8-supervision/20261018-handoff-v7/`；库custom dump：`owned-e2e-pg.dump`。交接时只对确认data_directory含本轮e2e标记的库只读导出成功，SHA见handoff-state。若原服务丢失，只能恢复到全新临时PG，禁止覆盖用户库。备份包含模拟/测试数据，不进入Git。

真实导入/分类/聚类/Insight已经完成12条，不要重复导入：

| 对象 | ID |
|---|---|
| 项目A | `aecbcfdf-d974-4aa7-b32d-a40543a95d6d` |
| 项目B | `58aa8ee7-9091-4fc6-a9e3-9e62c9a922fd` |
| 分类Run | `25875198-5239-49e7-b807-a4955e194591` |
| 聚类Run | `4c37b412-ef49-4fe8-8796-10dd0a909934` |
| Insight Run | `8e1f6b54-d7ea-4a7b-91ca-c7d328d151e3` |
| 照片Insight | `771392b6-80e5-480f-9d83-c40ff23fd1e3` |
| 当前PDF Insight | `20b8361c-8810-432e-9131-56937a0e5b95` |

旧照片链已真实走过Research→测试PM→AI Requirement→AI PRD，但PRD把反馈外流程写成事实，内容质量FAIL。旧Requirement `e5ecd321-c3de-4d6d-82d2-085b74c587b6`已archived；旧PRD `6d04df0d-f5b8-49be-a54f-4ad4c9b8909a`保留失败证据。

当前浏览器页面为 `/insights/20b8361c-8810-432e-9131-56937a0e5b95`。已发起问题：`According to official Microsoft Support, can OneDrive iPhone upload PDF files and what size limits apply?`，交接时只读API确认Run `53763da7-b422-47a3-adb5-0458aa4bc72b` 为 **partial_failed / object_unknown**。这不是成功Research。**PDF链尚无新PM/评分、Requirement v2、PRD v4；不沿旧日志拼接成成功链路。** 任意对象（如PDF）目前的契约表达能力也是待定位问题，不能为此硬编码该题答案或冒充已有通用支持。

Playwright会话：`/Users/salmon/.codex/skills/playwright/scripts/playwright_cli.sh -s=phase8v7`；先读技能、snapshot刷新refs。截图/最终浏览器证据存 `output/playwright/phase8-v7/`。页面原有无决策/评分的404空态曾出现，应区分空态与JS异常，最终仍核对console/network、桌面/390px和真实API数据。

Raw交接复核：12行、9字段 `content/created_at/external_id/feedback_created_at/id/project_id/source/user_id/user_type`，逐字段相等；规范UTC快照SHA `bf8ff4e186f47965f7ff1c99d845c145ab761f7cd4ce2ec72f3063deaf03fecd`。

旧模拟审批证据 `final-tool-api-db-audit.json`、`workflow-ids.json` 保留。**不要原样重跑 `/tmp/phase8_tool_audit_v7.py`**：它依赖旧已归档Requirement/旧计数，会再归档目标。新链须针对新Requirement重新写独立验证：Prepare→Cancel无目标；Prepare→Approve为SIM；重复Approve同SIM且单目标；新Prepare后approved→archived→Approve409；外项目全404。SIM只证明模拟，真实外部集成仍DEFERRED。

## 9. 下一对话的执行顺序与失败验收门槛

1. 现场核对本文指纹/HEAD/index、进程归属和临时库。确认无其他写入者；Codex亲手实施。先处理两文件格式红项，不清理已有工作。
2. 搜索诊断：为配置缺失、认证、限额/限流、网络、5xx、非法JSON/字段生成闭式安全分类及可重试标志；覆盖错误正文含密钥的反例，确保不泄露。准确核对服务实际配置加载链，不把独立shell缺配置当真实失败原因。
3. 选一个真正可供后端独立调用的替代provider，小规模冒烟：成功返回官方链接；失败分类正确；同Run总预算不重置；旧默认兼容。工具MCP结果不当后端接通证据。不可用就记录NOT MEASURED并继续可做的离线修复，不造可用性。
4. 冻结回放Dropbox原#2“已读但未提供”的每一步：catalog→applicability→分组/排序→offered预算→selection closure。增加实际失败反例后修准确层。以flow和前提为整体提供/选择，不混借其他流程；明确多选仍须独立数量证据。
5. 校验中文/多产品/analysis规划、query IDs和任意对象表达（PDF失败），保留原问分母及真缺证据。只做有原文依据的局部规范化，不扩关键词表、题目表、宽松匹配或全面拒绝。
6. 补材料化proof篡改反例，审查最新否定范围/owners修复正反例，确认旧保护测试未改、协议schema与runtime一致。
7. **最终同代码**完成SQLite全量、新临时PG全量、另一新临时PG迁移往返、Ruff check/format、前端lint/test/生产build；之后原十题+另八题真实完整调用，独立HTTP重新读官方原文逐题核对，再用同最终代码同集复验并公开差异。
8. 最新代码生产浏览器：Research→来源→支持/缺口→测试PM Decision→真实AI Requirement v2→reviewing/approved→真实AI PRD v4；逐章审核事实/提案/引用、响应延迟/项目切换/跨项目404、桌面/390px；不得预造approved需求替代链路。
9. 新目标模拟审批/项目隔离、最终Raw九字段审计、保护清单、脱敏证据和人工复测文档。正式用户验收不代签，历史Phase2/3/4/6/7限制保留。
10. 只有全部工程、功能、真实质量、链路、审批隔离及文档门槛满足才精确暂存并本地提交；任何阻断保持PARTIAL，不提交、不push。Phase8交付后STOP，不进入Phase9。

必须保留的反例：一般流程+局部否定、仅局部限制不能证明一般能力、真正正反/版本冲突；产品设备不交叉且共享设备不漏；多选缺证据不可借普通复数；条件不能漏/跨流程借；40/41引用边界与反证撤销；分析≤8、坏候选不占槽、依赖失效撤销；Raw映射冲突/proof篡改/超长不截；错误项目在模型或写入之前404；迟到页面响应不串项目。

## 10. 可复用命令和文件索引

有效Python（现有API环境，无需安装新依赖）：

`/Users/salmon/.cache/uv/archive-v0/guUL74lMTTwiFZLr3b7da/bin/python3`

`backend/.venv/bin/python`缺pgserver；系统默认python3曾指向其他项目，不使用。真实脚本自己按路径加载backend/.env及key/.env的JSON配置；禁止source JSON或打印凭据。复制脚本不含密钥，当前进程配置仍需重新确认。

在backend目录执行八核心：

```sh
/Users/salmon/.cache/uv/archive-v0/guUL74lMTTwiFZLr3b7da/bin/python3 -m pytest tests/test_phase8_spec_v7_supervisor.py tests/test_phase8_support_v7_supervisor.py tests/test_phase8_answer_v7_supervisor.py tests/test_phase8_catalog_v7_supervisor.py tests/test_phase8_flow_v7_supervisor.py tests/test_phase8_pipeline_v7_supervisor.py tests/test_phase8_frozen_v7_supervisor.py tests/test_phase8_review_v7_supervisor.py -q
```

全量PG/迁移脚本已备份为 `work/phase8-supervision/20261018-handoff-v7/phase8_codex_final_pg.py`，只创建自己的全新临时PG。其日志默认覆盖旧codex-pg日志，重跑前改用新批次输出目录，保留旧失败/通过证据。

真实题集脚本：`backend/scripts/verify_phase8_research_live.py --samples 10 --suite original|extended --prompt-version research-agent-v7`。每套独立新PG，先少量冒烟确定provider可用，再全套；不要在服务失效时盲跑多批消耗LLM。

前端务必工作目录 `/Users/salmon/Desktop/InsightFlow/frontend`，终端使用 `login:false`；生产build显式 `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:18080 npm run build`。不要把正确的构建产物和旧API进程混称同最终代码链路。

Raw脚本备份：`work/phase8-supervision/20261018-handoff-v7/phase8_v7_raw_snapshot.py`，backend目录、`PYTHONPATH=.`、参数`verify`；只针对自己隔离库。若恢复到另一新临时库，先改成新隔离DSN文件路径并核验归属，不碰用户库。

Worker历史账本：`work/phase8-supervision/20261017-implement-v7/jobs.json`。最后三次空Worker job为 `dd3cb1c530b4473b8b92156e1af1cb28`、`ea7974b61c3f48c4a3847fb2d5089358`、`2f2834f031e14c9e8daf8109e29bd36e`；AA1 `99dd451e4d344ade9f8f9c4ae2666df8`超时零写入。材料化模块由Codex创建，不归功Worker。

本地敏感/易失证据在work和output，均不得盲目git add。不要提交Raw快照、DB dump、DSN、密钥、ZIP或整个.playwright-cli。最终需要进入Git的证据须另行最小脱敏归档，逐文件审查后精确暂存。

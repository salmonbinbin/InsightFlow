> **历史启动文本说明（2026-10-17）：** 以下是v6实施前的启动Prompt。v6已实施并复验，当前仍PARTIAL/Research质量FAIL，未提交未推送；请以[42号报告](42-Phase8业务补齐实施与独立验收报告.md)及其中失败包/任务账本为现场续接依据。当前Phase8已STOP，不进入Phase9。

请担任 InsightFlow Phase 8 的 Codex Supervisor。项目路径：`/Users/salmon/Desktop/InsightFlow`。全程中文，称呼我“三文鱼”。

继续执行此前已授权的Phase8定位、修复、独立验收和满足全部门槛后的本地提交；禁止push，Phase8后STOP，不进入Phase9。旧Phase4等阶段文字不能取消当前Phase8范围，历史质量限制仍需保留。

先完整阅读：

1. `AGENTS.md`
2. `docs/40-Phase8业务缺口与新对话交接.md`
3. `docs/superpowers/plans/2026-10-17-phase8-research-prd-completion.md`
4. `docs/39-Phase8任务覆盖修复与独立验收报告.md`
5. 本启动文件

再按40号入口读38、36、37及31—35号报告/人工流程、AGENT_HANDOFF、技术开发规范与Git规则、工作记录、长期总纲、08/09、V1 PRD和原Phase8详细任务。以36号根因更正、39号最新实测和40号业务缺口为准；计划中的步骤是待执行，不能当成功能已实现。

当前Phase8为PARTIAL、Research质量FAIL、未验收、未提交、未推送。HEAD交接时为449e879、分支codex/phase6-evaluation，大量未提交改动，暂存区空；先现场核对Git差异/代码/迁移/数据库/API/进程/测试，保护现有工作，不根据旧聊天或Worker完成声明判断。先汇报现场核对和本批精确修复范围，再实施。

最近最终代码工程实测：SQLite1092通过/11跳过；全新隔离PostgreSQL1103通过且完整迁移往返；Ruff通过；前端154项及lint/build通过。原十题真实Tavily+MiniMax-M3为2 completed/8 partial_failed；独立官方原文核对2完整回答、6局部事实、2未验证，相关性误收1条。正式人工验收与Research后完整业务链路尚未验证。以上是上一轮实测，下一轮改动后必须重新验证。

特别重要：当前v5的research_selection.materialize把market_patterns、opportunities、risks固定为空，主要生成competitor来源摘录；自动完成主要依赖少数产品英文步骤问法。因此本轮不能只修完整性状态/提高completed数，必须补齐原需求中的Research业务能力、模式/机会/风险及支持关系。字段存在和页面区域存在不等于功能实现；真实资料不足可以明确空缺，不能在实现中永远固定为空。

按计划分批：

- 回放原十题归档与固定空分类，构建公开来源失败夹具。
- 建立保留原问题全部条件的任务契约与证据适用范围；支持中文/改写/能力/限制/多入口需要真实验证，不能按题号或完整问句查答案表。模型可辅助拆题，不能自评完整性。
- 补嵌套步骤/选项/条件、缺口驱动取证、空query去除、重复来源处理、summary/工具协议有界纠错。
- 恢复事实、模式、机会、风险等有依据的业务输出；区分事实/推断/建议，不凭confidence升confirmed，支持事实必须同Run/同项目。任何必要schema/DTO/持久化变化先由Codex评审，不默认加迁移、不修改旧迁移。
- 后端稳定后再续原前端会话；最终真实浏览器完成Research及来源→PM Decision→Requirement→PRD。不得把外部资料冒充Internal Evidence或伪造feedback引用，不默认自动修改PM决策。

保留v14精确引文、引文外扩写/否定、多选数量、官方来源ID/URL守卫；不得盲目反转义或宽松空白匹配。Dropbox内部双引号问题应通过等价严格的结构化引用契约解决，不能直接放行旧错误格式或删掉一步称完整。Apple正文存在却漏嵌套答案、手机问题误收电脑事实、iOS16手势限制不能完成通用多选流程，都要有行为回归。

预算沿用3逻辑搜索、5正文/来源、100提供证据单元、40选择、总LLM3次；若增加规划调用，算在总预算内。正文保持原官方allowlist，每跳HTTPS/userinfo/端口检查，最多3跳、10秒、解压后4MiB、文本24000字符。不能为提高完成率无限重试或放开任意URL。

Codex负责范围、架构、独立验收和Git；Research原Claude线程为`8d048dd4-fb66-4838-898b-44f10cedd34c`，前端为`42e973e0-5295-496d-a9bd-7c08d6d24ae4`。明确允许文件再下发，每个jobId立即保存。失败附具体Run、原问题、query、官方原文、模型/协议元数据和失败断言，续同线程。同一工作树一个写入者；Worker工作时Codex不改源码。Worker沿用edit/受控acceptEdits/simple，只允许Read Glob Grep Edit Write，禁止Bash/Git、Task/Agent、权限、AGENTS/CLAUDE、密钥、历史迁移与旧Prompt；测试由Codex执行，不靠改权限绕过。旧账本有4次Bash请求被拒绝，不能说零请求。

保护两份用户原型ZIP、Raw和密钥；用户数据库禁止downgrade、改权限、重启恢复，破坏性验证只用全新临时PG。两套用户库曾只读Operation not permitted，revision/行数NOT MEASURED；8000曾属于其他项目，先核对归属。Tavily曾因启动环境继承差异不可见，随后根目录真实调用已完成；先只检查配置存在性，不直接认定缺密钥，不输出值。key/.env是JSON，禁止source。

最终代码完成后：SQLite全量、全新隔离PG全量与upgrade/check→downgrade base→upgrade/check、Ruff、前端lint/test/build；真实FastAPI+生产Next Research/来源/覆盖/项目切换和跨项目404；模拟Prepare→Cancel→Prepare→Approve、重复同SIM且只有一个模拟目标，新Prepare后合法approved→archived再Approve409，Raw全字段快照不变。SIM只能称模拟，真实Jira/Notion/飞书/MCP可DEFERRED，不作为抢先接入目标。

保持原十题再做真实调用，逐条对照官方原文，分别记录完整/局部/未验证、事实/相关性/完整性误阳性。另建中文/改写/分析集单列，不能替换原分母。现有资料充分的题目不能因代码不支持仍当作“资料没有”；无明确多选证据时允许诚实缺口，不能强求10/10 completed而造假。声称稳定时按计划再做一轮同代码真实复验，公开两轮差异。配置存在、手造payload、Mock、模型自评分均不能替代质量证据。

更新工作记录、08/09、交付报告、人工流程和任务账本，保护/证据/最终代码做哈希校验。工程、业务功能、真实质量、完整核心链路、审批隔离和文档都满足才由Codex精确暂存并本地提交，禁止git add全仓，禁止push；仍有阻断就PARTIAL、不提交，保存失败证据。历史Phase2/3/4/6/7限制保留，正式用户验收不得代签。结束立即STOP，不进入Phase9。

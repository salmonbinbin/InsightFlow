# Phase8 新对话续接 Prompt

将下方正文发送给新对话。本Prompt与47号对应；44号的预算、保护、验收与Git门槛继续有效，但Claude写入方式已由用户新授权覆盖。

---

请继续完成 InsightFlow Phase8。项目路径 `/Users/salmon/Desktop/InsightFlow`。全程中文，称呼我“三文鱼”。

**最新授权：由Codex亲手修改源码、测试和文档，不再让Claude Code写代码，也不再续派原Claude线程。** 单工作树同时只有一个写入者；先核对进程和现有修改。不要重新询问是否继续，不要覆盖/清理已有工作。

先完整阅读：

1. `AGENTS.md`
2. `docs/47-Phase8当前进展痛点与新对话交接.md`
3. `docs/48-Phase8新对话续接与搜索替代方案Prompt.md`
4. `docs/45-Phase8统一证据契约设计与实施计划.md`
5. `docs/46-Phase8实施进度与恢复路径复核.md`
6. `docs/43-Phase8深度根因复盘与新对话交接.md`、44号约束及42号历史验收报告，再按43号索引读规范/工作记录/长期总纲/V1 PRD及修订/原Phase8任务/历史报告/人工复测。

当前HEAD `449e8790b8da8b9f04f4e3739cd9740606241cc4`，分支 `codex/phase6-evaluation`，暂存区空，大量修改未提交。Phase8仍PARTIAL、Research质量未过、正式用户验收NOT MEASURED。不得push，全部门槛满足才精确暂存和本地提交，Phase8交付后STOP，不进入Phase9。

v7已经实施，不要被旧入口“尚未实施/继续Claude”的历史说明带偏。Codex已亲手修复预算反证撤销、比较依赖失效、分析8条上限/候选来源、设备归属、scope、辅助规划归位；增加Requirement v2/PRD v4事实材料化/proof、8路由项目隔离和前端迟到响应防串项目。

最后两项修复：一般上传流程中的“不能上传iCloud优化照片”不再否定整个一般流程，但保留限制引文，只有限制句不能证明一般能力；Google Photos Android和Dropbox mobile不再被做成四格笛卡尔积。八核心最新230 passed。最后四文件Ruff check通过，但support_v7及其新增测试两个文件format仍红，须处理。完整SQLite1464/11skip、临时PG1475/迁移往返、前端182/lint/build是较早成绩，不能当最终同代码通过。

当前重点痛点：

- 第二轮原10和另8都未completed，每套各9次provider_failure事件；日志未留具体HTTP分类，不能断言额度/认证/网络。独立shell探针缺Tavily配置不等于真实服务没配置。
- MiniMax web_search工具已实测能找到微软官方页面，但只证明Codex工具可用，**未接通项目后端的独立HTTP搜索**。
- Dropbox正文已取到仍漏提供2–8步/前提；另一题全部已提供，却混绑多个流程并漏条件。要回放catalog→applicability→offered→closure→binding，而不是放松数量/条件/完整流程守卫。
- 协议/规划仍有action/product/version/analysis_facets/query ID错误；中文/分析集不能靠宽松归位换绿；PDF真实问题在当前浏览器Run中object_unknown，任意对象契约待定位。
- 新材料化与项目隔离的真实AI端到端质量尚未闭环，旧照片链PRD内容失败保留。

先现场核对47号和 `work/phase8-supervision/20261018-handoff-v7/handoff-state.json`，汇报本批精确范围，再实施。优先补安全错误分类、核对服务配置加载，设计provider可替换适配与总预算，再实际验证一个可在后端独立调用的替代服务。保留旧默认兼容，不打印/修改密钥，不以MCP可搜索冒充产品可用；必要时评估原问直接给官方URL的受控读取，但不能放十题URL/答案表。切换服务也受同Run搜索≤3，不能为每个provider重置预算；只把URL发现换入口，正文和支持守卫保留。

保留总LLM≤3（规划纠错全部计入）、搜索≤3、官方正文尝试/来源≤5、累计提供≤100、交付引用并集≤40、单片≤200，逐跳安全和官方政策不变。模型能提候选，不能自评完整性；原问义务、来源政策、事实、相关性、条件/数量/版本、分析支持和最终回答各有职责。事实/推断/建议分开，支持关系同Run同项目。不要堆关键词、题目答案表、宽松匹配或全面拒绝。

两轮真实冻结文件见47号：第一轮原10程序2completed/8partial，另8全partial；第二轮原10和另8全partial，同时有搜索/协议/规划问题。不是同最终代码稳定性对比，也没有完整新语义评分。原10保留分母，中文/改写/分析另8另列；completed不等于人工质量通过。

保留Raw、两ZIP、密钥、历史迁移0001—0015、旧Prompt/测试、AGENTS/CLAUDE和全部已有修改。破坏性验证只用全新临时PG，不动用户库权限/迁移/重启恢复。

当前自建API18080/生产Next13080与隔离PG交接时仍活跃，PID须重新查归属。控制目录 `work/phase8-supervision/20261018-phase8-v7-e2e/`；不要创建server-stop导致丢掉当前临时库。备份 `work/phase8-supervision/20261018-handoff-v7/owned-e2e-pg.dump` 只可恢复到全新临时PG；DSN不打印/不提交。有效Python为 `/Users/salmon/.cache/uv/archive-v0/guUL74lMTTwiFZLr3b7da/bin/python3`。脚本和前端日志已有work副本，不依赖/tmp永久存在。

当前PDF Insight `20b8361c-8810-432e-9131-56937a0e5b95`，Research Run `53763da7-b422-47a3-adb5-0458aa4bc72b` 已partial_failed/object_unknown；尚无此新链PM/Requirement v2/PRD v4，不冒充已完成。旧照片链失败及旧SIM留档，不能原样重跑依赖旧归档目标的审批脚本。12条Raw已导入，别重复；交接时9字段逐字段不变，完整ID和SHA见47号。

最终必须在同代码重新做全量工程检查、原10+另8真实Research及独立官方原文核对/同集复验、生产浏览器桌面/390px、Research→来源→测试PM→真实AI Requirement v2→reviewing/approved→真实AI PRD v4逐章支持检查、新目标模拟Prepare/Cancel/Approve/重复同SIM/归档409、项目API与页面隔离、Raw九字段与保护指纹审计。SIM仅模拟，真实外部集成DEFERRED，正式用户验收不得代签，历史质量限制保留。

任务目标是实现PRD/Phase8业务能力，不是只提高completed、增加UI区域或把测试变绿。全部门槛通过才本地提交，否则PARTIAL并保留失败证据、不提交、不push；只在Phase8边界STOP。

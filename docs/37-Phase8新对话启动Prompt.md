> **最新修复与复验（2026-10-17，v5）：Phase8 PARTIAL，Research质量FAIL，未验收、未提交、未推送。** 已实现保守任务/证据覆盖、官方正文、证据ID引用及安全诊断；最终SQLite1092/11skip、隔离PG1103与迁移往返、Ruff、前端154及lint/build通过。原十题为2完整/6局部/2未验证，复杂问题仍未闭合。详见[39号修复与独立验收报告](39-Phase8任务覆盖修复与独立验收报告.md)。以下保留旧阶段快照；36号根因更正仍有效。STOP，不进入Phase9。

请担任 InsightFlow Phase 8 的 Codex Supervisor。项目路径：`/Users/salmon/Desktop/InsightFlow`。全程中文，称呼我“三文鱼”。本次授权继续 Phase 8 定位、修复、独立验收与符合门槛后的本地提交；禁止 push，Phase 8 后立即 STOP，不进入 Phase 9。

先完整阅读 `AGENTS.md`、`docs/36-Phase8Research根因与新对话交接报告.md`、`docs/34-Phase8测试问题与新对话交接.md`、`docs/31-Phase8新对话开发交接.md`、`docs/32-Phase8架构验收与交付报告.md`、`docs/35-Phase8Research分层定位与本轮复验.md`、`docs/33-Phase8人工复测流程.md`，再按入口读取项目规范、工作记录、长期总纲和原任务。36号报告中的本轮复现及更正优先于旧归因；旧文件中的 Phase 2/4/6 阶段限制不能覆盖我当前的 Phase 8 授权。

当前为 PARTIAL、Research质量FAIL、未验收、未提交、未推送。交接时HEAD为449e879、分支codex/phase6-evaluation，工作树有大量Phase8未提交修改。先现场核对Git差异、代码、迁移、数据库、API、进程与测试，保护现有文件，不根据旧聊天或Worker完成声明推断状态，不擅自切换/清理工作树。

最近最终代码证据：Research283通过；SQLite972通过/11跳过；全新隔离PostgreSQL983通过且迁移往返通过；Ruff通过；前端147项、lint/build通过。真实Tavily＋MiniMax-M3十条为5 completed/5 partial_failed，Codex官方原文核对仅2完整回答/3局部事实/5未验证。真实浏览器Research/项目隔离和模拟审批有证据；SIM只能叫模拟，真实Jira/Notion/飞书及MCP未接入。这些都是交接前证据，后续修改后必须重验。

先回放 `docs/evaluations/phase8-v4-live-evidence.json` 和 `docs/evaluations/phase8-rootcause/`。已复现的主因是：至少一条合法事实即可completed，未验收问题覆盖；一次搜索只取snippet会漏官方关键段落；Apple搜索/en-us与允许的/guide来源范围错位；模型引文存在字面量换行、隐含长度/内部引号等表示问题；软降级后不进入纠错；协议失败缺安全响应元数据。旧报告两处需更正：Dropbox overnight句确在所引snippet，首先被数量门拦下；iCloud初轮是缺risks，并非多出字段。

不要继续只堆Prompt或关键词，也不要以完成率为目标放松v14的精确引文、引文外扩写/否定、多选数量、官方来源ID/URL守卫。先明确子问题与证据覆盖、核心/辅助事实、完整性门槛及兼容方案，再分批最小修复：官方证据检索闭环、保存原文与定位、确定性引用生成、有界具体纠错及安全协议观测。不能由模型自评完整性；不要盲目反转义后放行。36号报告是待验证方向，不代表这些功能已实现。

由Codex负责范围、架构、独立验收和Git；指挥原Research Claude会话 `8d048dd4-fb66-4838-898b-44f10cedd34c` 实现明确文件范围，每个jobId立即保存。失败带具体Run、实际query、来源原文、payload及失败断言，续接同threadId。前端确需变更才续接 `42e973e0-5295-496d-a9bd-7c08d6d24ae4`。同一工作树只有一个写入者。Claude不运行Git，不改AGENTS.md、CLAUDE.md、权限、密钥或历史迁移；沿用受控edit/acceptEdits/simple，测试由Codex执行。

保护两份用户原型ZIP、Raw Feedback、密钥和历史迁移；用户数据库禁止降级，迁移往返只用全新隔离库。Tavily曾因启动环境差异出现后端目录不可读、根目录可读，随后真实调用已完成；先只检查配置存在性，不再直接断言缺密钥。项目验证脚本读取TAVILY_API_KEY（进程环境或backend/.env），模型读取LLM_API_KEY/LLM_BASE_URL/LLM_MODEL，并可在隔离进程映射key/.env的既有配置。不要输出、修改或提交密钥；真实调用缺配置时标NOT MEASURED。

最终代码重跑SQLite全量、全新隔离PostgreSQL全量与完整迁移往返、Ruff、前端lint/test/build，再用真实FastAPI＋Next.js浏览器测Research、来源、项目隔离和模拟Prepare/Cancel/Approve、重复批准与仅一条模拟目标。保持原题集，抽查5—10条真实Research，逐题对照官方原文，区分完整回答、来源支持局部事实、未验证、事实及完整性误阳性。人工诊断payload、Mock、配置存在或模型自评分均不能顶替真实质量。

更新工作记录、验证记录、交付报告和job日志。只有工程、真实Research质量、审批/隔离和文档门槛全部满足才由Codex本地提交；仍失败就PARTIAL、不提交、不push，保存复测证据。保留历史语义/人工复核限制，不代签用户验收。先汇报现场核对与修复范围，再持续完成已授权工作，无需重复询问是否继续。

> **历史交接状态说明（2026-10-17）：** 本文件描述v5接手时缺口。后续v6已实施并独立实测，当前结论仍PARTIAL/Research质量FAIL，最新实现、质量失败和STOP边界见[42号报告](42-Phase8业务补齐实施与独立验收报告.md)；不能继续把本文件的“尚未实施”当作当前代码状态。原限制与36号更正保留。

# Phase 8 业务缺口与新对话交接

日期：2026-10-17。负责人：三文鱼。项目：`/Users/salmon/Desktop/InsightFlow`。

**当前状态：Phase8 PARTIAL，Research质量FAIL，尚未达到完整需求标准；未验收、未暂存、未提交、未推送。** 本次只编写后续计划和交接文档，没有新产品修复、模型调用、测试或数据库操作。新对话才继续实施，禁止push，Phase8后STOP，不进Phase9。

这份交接补充了39号报告未充分强调的业务缺口：**当前v5固定将market_patterns、opportunities、risks置为空；少数步骤题能完成不等于原Research需求完整实现。** 原39号实测及36号根因更正保留有效，不重写成新成绩。

## 1. 新对话先读

1. `/Users/salmon/Desktop/InsightFlow/AGENTS.md`。
2. 本文、[后续实施计划](superpowers/plans/2026-10-17-phase8-research-prd-completion.md)、[41号启动Prompt](41-Phase8业务补齐新对话启动Prompt.md)。
3. [39号最终修复与独立验收报告](39-Phase8任务覆盖修复与独立验收报告.md)、[38号已执行计划](38-Phase8任务证据覆盖契约与修复计划.md)。注意38号勾选只指已执行，不是质量PASS。
4. [36号根因报告](36-Phase8Research根因与新对话交接报告.md)、[37号旧启动Prompt](37-Phase8新对话启动Prompt.md)，再读31—35号交接/报告/人工复测；后者为历史分层证据，不能覆盖最新结果。
5. `AGENT_HANDOFF.md`、`技术开发规范与Git规则.md`、`工作记录.md`、`docs/11-长期开发总纲与Agent交接规范.md`、`docs/08-决策记录与修正.md`、`docs/09-验证与交付记录.md`、`docs/02-V1-PRD.md`。
6. 原Phase8详细任务：`/Users/salmon/.codex/attachments/bc5022c2-13c9-4896-b253-0a584536baa2/已粘贴的文本.txt`。如果附件路径失效，先从本聊天附件/记录找回原文，不凭摘要重新定义需求；实施计划第1节是对应表，不替代原文。

优先级：三文鱼最新Phase8授权 > 当前现场代码/证据 > 本交接与39号最新实测；36号针对旧归因的更正继续有效。旧AGENTS/入口中的Phase2/4/6 STOP不能取消当前Phase8范围，历史质量限制仍保留。

## 2. 产品目标与当前完成情况

InsightFlow把Raw Feedback经分类、聚类、Insight/Evidence、评分和PM Decision转成Requirement/PRD；Research为具体Insight补充公开竞品资料。外部研究与内部用户Evidence必须分表分区，Research不能修改最终决策或自动外部写入。

| 需求 | 当前状态 | 新对话处理 |
| --- | --- | --- |
| Research启动、历史、来源/发现查询、Insight UI | 已接通，真实API/浏览器验证 | 保留并回归 |
| Run/Source/Prompt追踪、任务/覆盖JSON | 已有迁移0012/0014及v5记录 | 不回写旧Run，兼容NULL |
| 内外证据分离、项目参数隔离 | 已测试 | 不变；不能扩称身份认证审计 |
| 精确引文/官方来源/多选数量等守卫 | 已实现，部分真实合法表达被保守拒绝 | 保留安全边界，修表达契约而非放宽校验 |
| 针对原问题完整回答 | 十题仅2完整；复杂条件/多入口常未闭合 | 最高优先级业务修复 |
| 模式、机会、风险、优缺点等分析输出 | v5三数组固定空，仅来源摘录 | 必须补功能及支持关系，不能只修完成数 |
| Tool registry、Preview/Cancel/Approve、幂等审计 | 本地SIM测试通过 | 保留，不抢Research修复范围 |
| Jira/Notion/飞书/MCP真实写入 | 未接入 | 原任务允许DEFERRED，不假装已集成 |
| Research→PM Decision→Requirement→PRD真实完整链路 | 本轮未完成新链路质量验收 | 最终独立补测，不能用预造approved种子替代 |
| 正式三文鱼验收 | 未签字 | 不能代签 |

PRD中92%、98%等展示示例不是本轮实测。当前2/10也只是固定题集结果，不能泛化为全系统成功率。不能因为工程全绿就说满足所有PRD需求，也不能说Phase1—7全部未实现；历史实现与未验质量分别记录。

## 3. 当前文件和已实现修复

- `backend/app/services/research_service.py`：active v5；保留v14严格校验，执行委派pipeline。
- `research_contract.py`：窄任务语法、证据目录、完整步骤/覆盖；未知条件不能丢。自动完成fullmatch主要是少数产品的英文步骤问法，不是一般研究解析器。
- `research_pipeline.py`：最多3逻辑搜索、5正文/来源、100给模型证据单元、40选择、3次LLM；有界预生成任务查询，尚非完整动态缺口闭环。`_queries`在多意图无法解析时可能产生只含site的无效第三查询。
- `research_reader.py`：既有官方域/路径边界，每跳校验HTTPS/userinfo/端口，3跳、总10秒、解压后4MiB、文本24000字符；保存正文和哈希/定位，保留alt。尊重既有代理环境，不改密钥配置。
- `research_selection.py`：模型只选ID，代码从保存原文提取引文。`materialize()`当前固定返回`market_patterns=[] / opportunities=[] / risks=[]`；所有选择都变成competitor摘录。此处是已确认业务能力缺口。
- `backend/app/agents/prompts/research_agent_v5.py`：闭合summary/confidence/evidence_ids，summary只允许内部上下文摘要；真实模型仍常写成外部答案并触发summary_invalid。
- `research_client.py`：安全metadata，白名单stop_reason/块类型计数/合法usage/HTTP状态；缺工具块不再只能看到统一异常，但协议稳定性尚未解决。
- `backend/alembic/versions/0014_phase8_research_coverage.py`：ResearchRun新增三个可空JSON；旧迁移未改。不要为下轮默认加0015，先评审分析类型/支持关系持久化需要。
- 前端：`frontend/src/lib/api/types.ts`、`frontend/src/app/insights/[id]/page.tsx`及测试；展示覆盖/缺口、中文内部错误提示、历史无报告未验证，保留“回答完整性：未验证”。真实形状是`kind=distinguish_auto,value=both`，不是`kind=both`。

最后后端行为修改是步骤span不吞前导空行、缺匹配官方全文时明确`official_snapshot_gap`；其后完成最终全量和最后十题，后续只有前端显示及文档更改。上一轮产品指纹见本机`work/phase8-supervision/20261017-repair/final-code-manifest.json`；新对话必须重新比对，不凭Worker声明。

## 4. Git、进程、环境、保护边界

交接编写前现场：HEAD `449e879`，分支 `codex/phase6-evaluation`；大量已有改动/未跟踪文件，暂存区空。不能擅自切分支、reset、clean、覆盖或一次性git add全仓。相对HEAD的差异含多轮已有Phase8实现，不全是本次修改。

保护两份根目录ZIP、Raw、`.env` / `key/.env`、AGENTS/CLAUDE、历史迁移/Prompt。key/.env是JSON配置，不是shell脚本；不能source，不打印值。正式用户数据库禁止downgrade、改权限、重启恢复。

最近现场事实：两套用户库`work/local-postgres`、`work/phase7-user-postgres`只读连接报Operation not permitted，revision/行数NOT MEASURED；没有修权限。8000属于其他项目；18080/13080曾用于自建隔离验收，上一轮结束已关闭。本次不启动服务，新对话先刷新归属再使用。

Tavily存在过启动cwd/环境继承差异，随后根目录真实调用已完成，不能直接断定缺密钥。真实脚本读取进程环境或backend/.env的TAVILY_API_KEY，模型读LLM_API_KEY/LLM_BASE_URL/LLM_MODEL；可在隔离进程内映射key/.env的既有ANTHROPIC_*字段。配置存在不算调用或质量通过。UV_OFFLINE只约束依赖解析，不禁止Python联网。

## 5. 最近最终工程证据（历史实测，不是新对话成绩）

统一本机目录：`work/phase8-supervision/20261017-repair/`。

| 门槛 | 结果 | 文件 |
| --- | --- | --- |
| SQLite全量 | 1092 passed / 11 skipped | final2-sqlite.log |
| 新临时PostgreSQL全量 | 1103 passed | final2-pg.log |
| 完整迁移往返 | upgrade/check→downgrade base→upgrade/check，POSTGRES_VERIFICATION_PASSED | 同上，含0012—0014 |
| Ruff | PASS | final2-ruff.log |
| 前端最终 | 154 passed / 15 files，lint/build PASS | final4-frontend-{test,lint,build}.log |
| 浏览器/隔离/模拟审批 | 真实FastAPI＋生产Next；Raw全字段相同；恰一SIM目标 | browser-api-audit.json、browser-db-audit.json、browser-final-*.log |
| 文件保护 | 38项哈希相同 | final-protection.json |
| 最终Git/代码审计 | 未暂存/提交/push；产品指纹 | final-audit.json、final-code-manifest.json |

本轮交接文档的只改文档证明另存`work/phase8-supervision/20261017-handoff-v5/`。不能把文档哈希检查称为重新运行后端、模型或浏览器。

## 6. 原十题最终质量与关键失败

最后后端代码数据：`work/phase8-supervision/20261017T031105Z/live-research.json`。已脱敏归档：[完整轨迹](evaluations/phase8-v5/live-evidence.json)、[官方核对](evaluations/phase8-v5/official-review.json)、[清单](evaluations/phase8-v5/manifest.json)。原十题见脚本`QUESTIONS`，不能改题美化。中间轮030450Z、旧v4/旧v14不混入最终分母。

**程序2 completed/8 partial_failed；来源核对2完整/6局部/2未验证；相关性误收1条。** 35条confirmed摘录有来源，不背书9条not_verified摘要；两条completed未发现整题误阳性，仍不能说Research整体PASS。

| # | 最终Run | 官方原文/实际断点 | 下一批回归 |
| --- | --- | --- | --- |
| 1 | `30882032-6718-4e08-ad3a-549d9dfbfee6` | Google多图Tips已取到并confirmed；复杂能力/手动自动区别未闭合 | 有原文时完整回答原问题，不能因窄语法一律拒绝 |
| 2 | `c641fdbc-f9a4-43e4-9482-b7151a1e61bd` | Dropbox结构/协议失败，未确认明确多选及限制 | summary/工具块分类纠错，不推断“不支持” |
| 3 | `12442ebb-49ab-419e-86ac-090590bddf6f` | OneDrive只确认iOS16手势限制/workaround | 保持局部，不误完成跨版本多图流程 |
| 4 | `db8baf49-5ffd-4af3-be46-54082d2347e2` | Shared Library原文有，三入口比较未完；电脑多格式句误收进手机问题 | 平台/入口/数量语境绑定；真实原文也可能无关 |
| 5 | `4fafa0e8-365b-4dff-af04-762dfa3b75aa` | Drive普通步骤不能证明明确多选；无confirmed | 保留数量门，区分资料不足与执行失败 |
| 6 | `c18a1d92-b011-42ac-b3ca-db9bc1e4e995` | 手动单张四步有原文，自动/手动区别证据未绑定 | 核心子问题分别证明，不能只验证步骤条数 |
| 7 | `1851b097-eda9-4715-a126-282946d38377` | Dropbox八步的第3步含内部引号被降级；条件句缺确认；两个URL重复摘录 | 结构化精确引文容器与条件绑定，不能删掉一步或放松守卫 |
| 8 | `931d236c-d887-4f9b-a3db-6286adedee91` | OneDrive Choose Existing→Photos→选择→Done，完整窄回答；前面仍有结构重试 | 正例不能回退；不扩写成明确多选能力 |
| 9 | `65a03fd5-780a-49ce-9069-8a70d57122ab` | Apple guide已取到，只确认顶层3步，手机三个选项及Shared去向未选；多意图解析失败/空查询 | 嵌套选项/注释保真和多要求覆盖 |
| 10 | `b1b24f32-1656-4f2c-b9d6-a0b58d6c56e7` | Drive Android同节四步完整 | 保持完整正例，不外推明确多选 |

### 关键官方页面

- Google Photos：<https://support.google.com/photos/answer/6193313?hl=en&co=GENIE.Platform%3DAndroid>
- Dropbox：<https://help.dropbox.com/create-upload/upload-mobile>
- OneDrive手动：<https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios>
- OneDrive iOS16故障：<https://support.microsoft.com/en-us/onedrive/troubleshoot-onedrive-for-ios-app-problems>
- Apple guide：<https://support.apple.com/guide/icloud/upload-and-download-photos-mmb5dbaf376f/icloud>
- Drive Android：<https://support.google.com/drive/answer/2424368?hl=en&co=GENIE.Platform%3DAndroid>

官方页面会变化；保存抓取时间和快照，用独立页面读取复查。简易HTML解析器与生产reader的图片alt空格处理不同，旧核对中个别字符串比对false不是自动判定造假，更不允许据此放宽生产引文匹配。

## 7. 原Worker会话与账本

- Research线程：`8d048dd4-fb66-4838-898b-44f10cedd34c`。
- 前端线程：`42e973e0-5295-496d-a9bd-7c08d6d24ae4`。
- Tool历史线程：`520a4b77-2518-4ca2-9fc9-439b21245afb`。默认不再改Tool，除非有独立新失败证据。
- 本轮11个job完整状态：[jobs.json](evaluations/phase8-v5/jobs.json)；实际写入工具审计：[worker-audit.json](evaluations/phase8-v5/worker-audit.json)。

Research最近完成metadata修复job：`40a9c7a2b8e84fdab69111d226d87ccb`。契约初稿返工job `7c5a609d02954c77a2400a8e52fcb89e`、`9b299a01bdf9459dbd9d7735e4e55284`失败/中断后Codex接管契约/管道；reader初稿job `98ab51bbb5be47cd809e37d69be6ee76`也按failed记录，不当完成证据。前端最近完成job `5b3704fcd9f849e89cd4f16318c17270`，修真实kind/value展示。交接前账本无活动job，新对话仍需现场查实际进程。

曾4次请求Bash（ls/wc/cat/tail），均被工具层拒绝，未执行；不能宣称零越界请求。沿用禁止Bash的边界，测试由Codex执行，不通过权限修改让Worker跑起来。

调用约束：使用原线程`claude_reply_start`，workingDirectory为项目根，tier=edit，runtimeProfile=simple，allowedTools=`Read Glob Grep Edit Write`，disallowedTools=`Bash / Task / Agent / NotebookEdit / mcp__*`；每批prompt明确允许文件与失败证据。不要启用bypass，不创建新Research线程来丢失原修复上下文。

## 8. 新对话应按什么顺序做

1. 现场保护/进程/API/迁移/测试核对，回放原十题与固定空分类；先汇报当前差异和精确修复范围。
2. 执行[实施计划](superpowers/plans/2026-10-17-phase8-research-prd-completion.md)Task A—B：任务/条件/语境契约，正例与反事实独立行为测试；解决只支持少数英文步骤模板的范围问题。
3. Task C：真实缺口驱动的取证、嵌套原文结构、引用表达、协议错误，保持全部严格守卫及成本预算。
4. Task D：真实实现模式/机会/风险及事实支持关系。将事实、推断、建议分开，不能靠字段存在或人工mock验收。
5. Task E：后端稳定后原前端会话接线；走真实Research后PM Decision→Requirement→PRD链路，模拟审批/隔离/Raw另验。
6. Task F：最后代码完整工程与迁移往返、原十题独立官方核对、另列中文/改写/分析集，稳定性复验。更新文档和保护审计；门槛未过继续PARTIAL、不提交。

当前没有可直接复用的在线临时Run数据库，旧UUID主要用于冻结证据。`/tmp/phase8_v5_browser.py`和`/tmp/phase8_source_review.py`只可作为本机参考，可能消失且有旧输出目录/种子；**不要直接执行覆盖上一轮证据，也不要依赖它们作为交接唯一工具**。新对话按33号人工流程与计划建立本轮新临时库/日志目录，代码已有verify_postgres/live脚本可用。

## 9. 完成门槛与人工边界

可提交须同时满足：最终工程与迁移通过、Research业务分类真正实现、原题/新增集真实质量过关、核心链路及模拟审批/隔离通过、文档/保护审计齐全。不能要求外部文档没有的答案，但有答案却没取到、代码不支持或协议失败必须作为功能/执行缺口，不能改成“资料没有”。

真实集成继续DEFERRED，SIM只是模拟。正式三文鱼签字、长期质量指标仍不得代签；Phase2真实语义Embedding、Phase3人工语义、Phase4业务评分、Phase6Artifact/完整链路质量、Phase7稳定性等历史限制继续保留。本轮没有解决这些历史限制，也不能顺带推进下一阶段。

只有门槛满足才由Codex审查精确暂存范围并本地提交，禁止push。无论最终完成或保持PARTIAL交付，Phase8后STOP，不进入Phase9。

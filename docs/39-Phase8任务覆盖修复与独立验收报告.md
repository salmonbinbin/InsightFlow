> **最新交接（2026-10-17，业务能力补齐）：Phase8仍为PARTIAL，Research质量FAIL，未验收、未提交、未推送。** 下一轮先读[40号业务缺口与交接](40-Phase8业务缺口与新对话交接.md)、[待执行处理计划](superpowers/plans/2026-10-17-phase8-research-prd-completion.md)和[41号启动Prompt](41-Phase8业务补齐新对话启动Prompt.md)。除复杂问题覆盖/协议稳定性外，当前v5固定将市场模式、机会、风险数组置空，业务功能未完整实现。本次只更新交接文档，不是新代码修复或新测试结果；39号历史实测及36号根因更正保留。禁止push，Phase8后STOP，不进入Phase9。

# Phase 8 任务覆盖修复与独立验收报告

日期：2026-10-17。负责人：三文鱼。执行：Codex Supervisor，续接原 Claude Research / 前端会话。授权止于 Phase 8，禁止 push。

**结论：Phase 8 PARTIAL；Research 质量 FAIL；未验收、未暂存、未提交、未推送。STOP，不进入 Phase 9。** 本轮完成架构修复、最终工程回归、真实十题来源核对及浏览器/模拟审批验收，但没有满足 Research 质量门槛。工程通过不能代替质量或三文鱼人工签字。

## 现场与保护

HEAD 仍为 `449e879`，分支仍为 `codex/phase6-evaluation`；保留接手时大量未提交 Phase 8 文件，没有切换、清理或覆盖工作树。初始差异、约280文件哈希、各批失败及最终验证保存在 `work/phase8-supervision/20261017-repair/`。以36号根因更正为基线，重新回放归档证据后实施；旧 v4 的 5 completed 不能当作5个完整答案。

保护审计：两份用户原型 ZIP、配置密钥文件、AGENTS/CLAUDE、旧迁移、旧业务 Prompt、锁文件及原始数据等38个文件全部哈希不变。迁移0012/0013未改，新增0014。旧 Research Prompt v1—v4保留。现有 Phase8测试只因主动版本/完成语义变化更新预期，保留严格引文、来源、数量和项目隔离反例；没有改写 Phase1—7测试获取绿色结果。

两套用户 PostgreSQL 只读连接仍报 `Operation not permitted`，其实际 revision / 行数为 **NOT MEASURED**；未降级、重启、改权限。8000属于其他项目，未干预。全部破坏性迁移和浏览器种子只用全新临时库。Tavily及模型配置存在性经检查，隔离进程真实调用完成；未输出、修改密钥。配置存在不计质量通过。

## 已实现与实际边界

1. `research_contract.py` 保存原问题、核心要求、未知子句与证据覆盖报告；同来源、同节、完整编号序列才可证明步骤。漫游设置、单一步骤、iOS16手势故障、跨节/重复步骤均不能替代目标流程。**目前只支持有限的完整步骤语法**，开放能力、条件、限制、多入口问题不能自动闭合，必须未验证。不能称为通用语义覆盖系统。
2. `research_pipeline.py` 将 Run 完成条件改为“至少一条严格合法事实 + 服务端覆盖通过”。模型 confidence / summary 不参与完成判定。补搜最多3个逻辑查询、正文最多5页、保留最多5来源、提供最多100证据单元、模型最多选40个ID、LLM最多3次。查询是有界预生成的任务扩展，可在已有完整流程时提前结束；**还不是按每次模型失败动态规划的通用闭环**。
3. `research_reader.py` 在既有官方 host/path allowlist 内读取正文，每一跳重验HTTPS、无userinfo、443端口，最多3次跳转、总预算10秒、解压后4MiB、文本24000字符。保存正文快照、时间、哈希、章节/编号和图片alt按钮名。Apple初搜覆盖整个官方域，过滤仍守原允许路径，未扩大来源白名单。使用既有代理环境，未改配置。
4. 新 `research-agent-v5` 仅输出 summary / confidence / evidence_ids；代码从保存快照和offset重新提取引文，再运行原v14精确引文、引文外扩写/否定、多选数量、名称及官方ID/URL守卫。没有盲目反转义、宽松匹配或删掉内部双引号限制。旧 closed v4 payload兼容入口也经过新覆盖门槛，没有测试专用放行。
5. 软降级/结构错误可将具体 first failed guard、候选索引和缺失要求ID反馈给下一次模型调用；协议失败仅存白名单 stop_reason、块类型计数、合法usage和HTTP状态，不存响应text/thinking/headers或任意标识符。真实记录显示仍有 summary_invalid / 缺工具块，不能宣称协议稳定性已解决。
6. `0014_phase8_research_coverage.py` 仅给 ResearchRun增加可空JSON `task_contract / coverage_report / execution_trace`；旧行保持NULL。前端字段可选/可空，展示服务器逐项覆盖及缺口、历史无报告未验证，始终保留“回答完整性：未验证”；内部诊断以中文提示展示，不把模型摘要当作已证实事实。

## 最终代码工程验证

后端最后两处行为修复为：步骤span不吞前导空行；有合法选择但无匹配官方全文时明确报告 `official_snapshot_gap`。以下全量与真实十题均在这两处修复后执行；随后只有前端显示层及文档改变。

| 门槛 | 本轮最终实测 | 日志 |
| --- | --- | --- |
| SQLite全量 | **1092 passed / 11 skipped** | `final2-sqlite.log` |
| 全新隔离 PostgreSQL全量 | **1103 passed** | `final2-pg.log` |
| 完整迁移往返 | upgrade/check → downgrade base → upgrade/check；`POSTGRES_VERIFICATION_PASSED` | 同上；含0012—0014 |
| Ruff全范围 | **PASS**，app/tests/alembic/scripts | `final2-ruff.log` |
| 新增核心模块格式 | **11 files already formatted** | 本轮独立检查 |
| 前端测试 | **154 passed / 15 files** | `final4-frontend-test.log` |
| 前端lint / 生产build | **PASS**，无lint警告，11路由 | `final4-frontend-lint.log` / `final4-frontend-build.log` |
| Git差异空白检查 | PASS | 最终 `git diff --check` |

执行方式：后端 `UV_OFFLINE=1 uv run pytest -q`、`UV_OFFLINE=1 uv run --with pgserver==0.1.4 python scripts/verify_postgres.py`、`UV_OFFLINE=1 uv run ruff check app tests alembic scripts`；前端 `npm run lint`、`npm test -- --reporter=dot`、`NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:18080 npm run build`。UV_OFFLINE只约束依赖解析，真实 Python HTTP/模型仍联网。SQLite跳过项不能算通过；由隔离PG全量覆盖相应PG场景。

## 原十题真实质量审查

最终后端代码真实调用：Tavily + MiniMax-M3，Prompt `research-agent-v5`。原十题未换题。程序 **2 completed / 8 partial_failed**；Codex逐条对照保存正文并独立HTTP重取关键官方页面，判 **2完整回答 / 6局部事实 / 2未验证**。浏览器另外两条不计入该分母。中间轮 `20261017T030450Z` 不是最终成绩。

| # | 最终Run ID | 判定及官方原文核对 |
| --- | --- | --- |
| 1 | `30882032-6718-4e08-ad3a-549d9dfbfee6` | 局部。Google Photos官方Tips的“back up multiple photos or videos … Touch and hold … tap Back up”已进入正文并confirmed；整题手动/自动区别与能力契约未闭合，保持partial。 |
| 2 | `c641fdbc-f9a4-43e4-9482-b7151a1e61bd` | 未验证。无confirmed，summary_invalid→协议失败→summary_invalid。Dropbox手动上传步骤的普通复数不能替代严格多选数量及限制证据。 |
| 3 | `12442ebb-49ab-419e-86ac-090590bddf6f` | 局部。iOS16两指滑选问题及workaround有Microsoft原文；只在该故障/版本范围成立，不能回答手动多图上传整题，未再误completed。 |
| 4 | `db8baf49-5ffd-4af3-be46-54082d2347e2` | 局部。Shared Library多选原文存在；三入口比较未完成。另收录“from your computer … multiple formats”句，事实真实但与iPhone目标不相关，记**相关性误收1条**。 |
| 5 | `4fafa0e8-365b-4dff-af04-762dfa3b75aa` | 未验证。Google Drive普通上传步骤存在，但没有满足数量守卫的明确多选事实；无confirmed，不推断产品不支持。 |
| 6 | `c18a1d92-b011-42ac-b3ca-db9bc1e4e995` | 局部。Google手动单张备份四步均有正文；核心“区别自动备份”未被确认证据覆盖，partial。 |
| 7 | `1851b097-eda9-4715-a126-282946d38377` | 局部。Dropbox八步中七步通过，两个URL重复共14条confirmed；第3步内部双引号仍被v14拒绝，camera uploads disabled条件没有成为确认事实。 |
| 8 | `931d236c-d887-4f9b-a3db-6286adedee91` | **完整窄回答 / completed**。Microsoft手动页提供顶部Add→Upload file/photo；Choose Existing进入设备Photos，选图片→Done。四条确认事实覆盖原题，不扩写所有版本或明确多选能力。 |
| 9 | `65a03fd5-780a-49ce-9069-8a70d57122ab` | 局部。Apple guide已读到；模型只确认前3个顶层步骤，没有选出手机的Photo Library / Take Photo or Video / Choose Files和Shared Library使用Photos App两项关键答案。计划为unparseable_multi_intent，保持partial。 |
| 10 | `b1b24f32-1656-4f2c-b9d6-a0b58d6c56e7` | **完整窄回答 / completed**。Drive Android同节四步全部确认：打开Drive→New/Upload→找到并点选文件→My Drive查找。原文图标alt为Add question，未把普通复数扩成明确多选。 |

35条confirmed均核对来源正文，未发现捏造原文的事实误阳性；两条completed未发现整题完整性误阳性。该统计**不背书not_verified摘要**，其中仍有原文不支持的外推；第4题相关性误收另列，不能称语义质量全绿。独立HTML简易解析器与生产reader对图片alt两侧空格处理不同，#6第一句和#9两句字符串审计显示false，人工核对实际按钮文字/正文后确认是表示差异；没有据此放宽生产引文守卫。

公开证据归档：[完整真实轨迹](evaluations/phase8-v5/live-evidence.json)、[独立官方原文核对](evaluations/phase8-v5/official-review.json)、[SHA256清单](evaluations/phase8-v5/manifest.json)。包括原问题、query、过滤前后来源、保存正文、每次结构输出/协议元数据、守卫/重试、选择和最终覆盖。已扫描本机已知密钥值，未命中。原始中间轨迹和独立页面文本留本机work目录。

## 真实浏览器、API、数据库验收

使用新建临时PG迁移到0014、真实FastAPI 18080、生产Next.js 13080；项目A/B、Insight和已批准Requirement为明确合成种子，不代表真实CSV→AI→PRD链路验收。

- 浏览器真实点击Drive原第10题，Run `3bbaaf48-83eb-444a-84bf-6c30507a861c` completed，2来源，逐项覆盖及完整性未验证均可见；展开引用看到对应Google官方来源。
- 浏览器再运行Photos原第6题，Run `a423ba26-2121-4915-9eba-bab164e465af` partial_failed；最终构建复查页面显示“区分手动与自动操作 · 未验证”和中文证据缺口，内部诊断码未直接显示。
- 初版与最终构建均切到B清除A的Research内容。两次Run、Sources、Findings及Insight Research历史携带B项目ID全部404。这是显式项目参数隔离验证，不是身份认证或完整租户安全审计。
- 浏览器Prepare→Cancel显示无模拟结果，再Prepare→Approve生成 `SIM-fbbc8718e39e44948de9d421`。API重复Approve返回同一编号；Cancelled Run批准409；跨项目读/取消/批准404。
- 第三次Prepare后，合法 `approved → archived` 撤销资格，再Approve返回409，随后取消。测试最初尝试approved→reviewing被现有状态机409拒绝，属验收脚本选择错误，已记录并改用合法归档；未修改生产状态机。取消后的两个Run均无external_id，最终DB恰好**一条SimulatedIssue**。
- Raw Feedback全部字段前后快照相同。自建API、两次Next进程及临时PG已关闭，18080/13080无监听；未触碰用户服务。

证据：[API审计](evaluations/phase8-v5/browser-api-audit.json)、[数据库审计](evaluations/phase8-v5/browser-db-audit.json)；本机 `browser-final-research.log`、`browser-final-project-b.log`、`browser-tool-*.log`、`browser-raw-before/after.json`及截图。浏览器控制台的favicon404和故意跨项目404另行区分，没有把这些当作研究执行异常。

**SIM只为数据库模拟**；真实Jira、Notion、飞书和通用MCP写入继续DEFERRED。

## Worker监督与修复证据

Research沿用 `8d048dd4-fb66-4838-898b-44f10cedd34c`；前端沿用 `42e973e0-5295-496d-a9bd-7c08d6d24ae4`。每批仅明确文件，edit / 本地acceptEdits / simple，禁止Bash、Git、Task/Agent、密钥/权限/AGENTS/CLAUDE/旧迁移；Codex独立测试。全部jobId见[任务账本](evaluations/phase8-v5/jobs.json)。

初契约两次返工超时/中断后Codex接管契约与管道实现，Worker结束前无Codex源码写入。Reader与元数据初稿独立出现5项测试失败，续原Research会话修复。前端曾有fixture文本断言/JSX引号失败，随后真实浏览器发现内部诊断和程序value直显，再续同会话；最终以真实kind=distinguish_auto,value=both而非虚构kind=both测试修正。Worker自述均未当作验收。逐条审计11个job输出，发现4次Bash请求（ls / wc / cat / tail），全部由工具层以Bash未启用拒绝，没有执行；不宣称零越界请求。实际写入路径均在对应批次研究/前端文件内，未见执行Git、权限或密钥写入。详见[Worker工具审计](evaluations/phase8-v5/worker-audit.json)，当前无活动Worker。

## 未关闭的质量问题与复测方向

1. 任务契约目前对多选能力、条件、限制和多入口问题保守拒绝整题完成。它修复了旧“一条合法事实就完成”的误报，但并未实现这些问题的可交付回答；不能靠把它们全部标未验证宣称质量修复成功。
2. #6步骤齐全但自动/手动区别仍缺证据绑定；#9正确正文已经存在，嵌套选项/限制没有进入确认结果。后续应建立这些类型的可验证要求与证据绑定，而非按题号写答案或放宽守卫。
3. #2/#5/#8仍出现summary_invalid及工具块缺失；安全协议元数据已可追踪，但结构稳定性尚未达标。#7内部双引号不能以宽松引用绕过。#4相关性误收说明“原文真实”仍不足以证明目标相关。
4. 多意图无法解析时第三候选查询可能退化为仅site的无效查询（#9留有provider_failure）；应在后续最小修复中抑制空任务词查询，不能把该次调用解释为缺密钥。

复测继续保持原十题与v14所有守卫，先用归档形成失败行为样例，再按明确文件范围续原Research会话。发生后端行为修改后，重新执行SQLite、全新PG全量及完整迁移往返、Ruff，再真实十题逐题官方核对和浏览器验收。不能用手造payload或模型自评填补质量分母。

## 最终Gate与STOP

| 门槛 | 结论 |
| --- | --- |
| 当前工程全量与迁移 | PASS，限所述自动检查 |
| 真实Research质量 | **FAIL**，2/10完整，相关性误收1；复杂要求仍未闭合 |
| 项目隔离及模拟审批 | PASS，限真实浏览器/API/隔离PG和本地SIM |
| 文档/证据归档 | 已更新；不是人工签字 |
| 正式用户验收、完整CSV→AI→PRD链路、本机用户库revision/行数 | **NOT MEASURED** |
| 真实外部写入 | **DEFERRED** |

Phase2真实语义Embedding、Phase3人工语义复核、Phase4正式业务评分、Phase6未测Artifact/完整链路质量、Phase7稳定性与人工验收等限制继续保留。**综合PARTIAL，不提交、不push。Phase8在此STOP，不进入Phase9。**

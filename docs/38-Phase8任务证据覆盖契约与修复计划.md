> **最新交接（2026-10-17，业务能力补齐）：Phase8仍为PARTIAL，Research质量FAIL，未验收、未提交、未推送。** 下一轮先读[40号业务缺口与交接](40-Phase8业务缺口与新对话交接.md)、[待执行处理计划](superpowers/plans/2026-10-17-phase8-research-prd-completion.md)和[41号启动Prompt](41-Phase8业务补齐新对话启动Prompt.md)。除复杂问题覆盖/协议稳定性外，当前v5固定将市场模式、机会、风险数组置空，业务功能未完整实现。本次只更新交接文档，不是新代码修复或新测试结果；39号历史实测及36号根因更正保留。禁止push，Phase8后STOP，不进入Phase9。

# Phase 8 任务与证据覆盖契约及修复计划

日期：2026-10-17。执行授权：三文鱼本轮 Phase8 定位、修复、独立验收、满足四门槛后本地提交；禁止push，不进入Phase9。

## 现场与选择

HEAD=449e879，分支codex/phase6-evaluation，大量已有Phase8修改保留。初始清单与差异保存work/phase8-supervision/20261017-repair/。归档replay/controls重新复现九条结构输出及旧完成门槛；漫游开关与孤立一步均可误完成。配置检查为CONFIG_PRESENT_NOT_CALL_TESTED；不是新真实调用成功。8000属于其他项目，不干预。

仅补Prompt不能建立覆盖约束；让模型自报complete仍缺独立依据。本轮采用“服务端任务契约 + 原文位置证据 + 保守覆盖门槛”。不声称解决开放域语义蕴含；无法确定解析的任务必须保留未验证。用户已授权该方向，不增加重复审批。

## 架构契约

1. TaskPlan保存原问题、产品、平台、入口、目标操作、数量、步骤、区别/限制要求与版本范围。每个子问题有稳定ID、核心/辅助角色和验收谓词。不能按十题ID或完整问题查表输出答案；未支持的问题解析fail-closed，不丢条件再完成。
2. EvidenceUnit只从保存的来源快照产生，有source_id、offset、逐字text、所属section和语境。模型只选择ID/位置；代码提取引文。未知/跨来源/越界/转义文本均不能直接成为已证实引文。保留v14精确引文、引文外否定/扩写、数量及官方host/path守卫。
3. 覆盖与事实合法性独立。核心步骤必须来自相应操作/平台/入口章节，覆盖完整流程而非任一动作；自动/漫游、同步、手势故障workaround不能替代手动上传。多图能力需核心操作内明确数量，普通复数不够。多个入口逐项绑定，不以一个入口推其他入口；没有资料不等于不支持。
4. coverage_report记录每项covered/partial/not_verified、证据位置和缺口；模型自评/confidence不参与完成门槛。unsupported_contract或未覆盖核心任务不能completed。历史无报告的Run仍显示未验证，不能回写旧Run。
5. 允许新0014仅为ResearchRun新增可空JSON审计字段（task/coverage/trace）保存契约和各轮取证/反馈，旧行NULL。不得重写0012/0013。API仅增加可空字段，前端兼容旧行；正式人工复核仍NOT MEASURED。
6. 官方取证仅现有allowlist。初搜后按缺口最多2次补搜（总3次逻辑查询），每次传输至多原3次；正文读取至多5页、每页超时10秒、解压后响应上限4MiB、文本上限24000字符、最多3次重定向，每跳重验官方host/path、https与无userinfo/非默认端口；禁止模型给任意URL。保存正文与摘录的区别、来源URL、时间、哈希及定位。失败留证据缺口。
7. LLM总调用保持最多3次；结构失败或软降级给固定安全错误码、失败候选索引及缺失子任务ID，不回传任意异常/Raw/密钥。协议元数据仅允许枚举stop_reason、block类型计数、HTTP状态、合法usage计数，不保存text/thinking/headers/请求密钥。

## 分批实施与验收

- [x] A：新纯任务/覆盖模块与独立行为测试；先验证漫游、单步、iOS16、未知条件和缺证据均不能整题完成。
- [x] B：有界官方正文与补搜，所有入口仍过原来源守卫；Apple guide范围；测试重定向、超限、超时、错误平台及缺段。
- [x] C：新版本证据选择输出、确定性引用生成，服务接线、0014与可空API字段、安全纠错和协议元数据；旧Prompt保留。全部v14负例继续有效。
- [x] D：后端稳定后续接原前端会话，仅展示真实覆盖项与缺口/旧Run未验证，保持项目切换清理。
- [x] E：最终SQLite全量、新隔离PG全量与upgrade/check→downgrade base→upgrade/check、Ruff、前端lint/test/build；真实FastAPI+Next.js Research/来源/隔离/模拟Prepare-Cancel-Approve及Raw快照。
- [x] F：固定十道原题真实Tavily+MiniMax，逐条官方原文核对完整/局部/未验证及事实/完整性误阳性。诊断payload不计真实质量；稳定质量未过仍PARTIAL、不提交。

Worker固定Research threadId=8d048dd4-fb66-4838-898b-44f10cedd34c。每批仅明确文件，禁止所有Bash/Git、权限、密钥、AGENTS/CLAUDE、旧迁移/旧Prompt及Raw写入。Codex执行测试；Worker期间Codex只读，任务登记为必要独立审计。失败带具体原文、Run与断言续同会话。

## Gate与历史限制

工程、真实Research质量、审批隔离、文档全部满足才由Codex检查暂存并本地提交。两份ZIP、密钥、Raw、临时产物不提交。SIM只模拟；外部Jira/Notion/飞书/MCP deferred。Phase2真实语义Embedding、Phase3人工语义、Phase4业务评分、Phase6未测指标、Phase7模型稳定性与人工验收限制保留。

## 本轮执行结果

A—F已执行，勾选仅指实施/验证已运行，不表示质量通过。任务计划只对有限完整步骤语法可自动闭合，复杂能力/条件/多入口仍需人工复核；补搜为有界任务扩展，非通用动态规划。最终SQLite1092/11skip、隔离PG1103与迁移往返、Ruff、前端154及lint/build通过。真实原十题2完整/6局部/2未验证，质量FAIL。浏览器/隔离/模拟审批通过，Raw全字段不变。Phase8 PARTIAL，不提交、不push，STOP。详见[39号报告](39-Phase8任务覆盖修复与独立验收报告.md)。

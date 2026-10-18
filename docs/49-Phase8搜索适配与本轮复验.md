# Phase8 搜索适配、入口作用域与本轮复验

日期：2026-10-18。三文鱼授权 Codex 亲手实施；本轮不派 Claude、不续原线程。**本轮收尾：Phase8 PARTIAL，Research质量FAIL，正式用户验收NOT MEASURED；不暂存、不提交、不 push，不进入 Phase9。** 三文鱼最新要求放过不影响正常需求的小问题；重复文案、按钮alt和细小排版不作为阻断，正常请求无法回答、错流程/条件及缺分析仍是主要限制。47/48号及45号预算/保护边界继续有效。

## 现场与本批范围

HEAD `449e8790b8da8b9f04f4e3739cd9740606241cc4`，分支 `codex/phase6-evaluation`，index空。交接六文件指纹全一致，保存237文件基线；已有未提交工作未覆盖。原API18080/Next13080/launcher与自建PG仍在；交互式Claude进程存在但处于等待状态，未派任务、未擅停。所有破坏性检查另起全新临时PG。

本批限定：两文件格式；搜索闭式安全诊断；v7可配置独立HTTP适配与共享预算；有真实反例的同流程入口语境和格式限定对象表达；proof篡改核对；随后完整工程、真实题集、浏览器及业务链。旧Prompt/迁移0001—0015不改，Tavily旧默认保留，无新增依赖或迁移。

## 搜索设计与实际证据

- `research_provider.py` 保留原客户端/异常类型，新增安全分类：authentication、quota（HTTP402）、rate_limit、upstream_server、request_rejected、redirect_rejected、timeout、network、invalid_json、invalid_response、application_error。只保存固定码、HTTP状态、retryable、物理尝试次数/耗时；不保存响应正文、请求头、异常原文或链。
- `research_search_adapters.py` 新增 MiniMax Coding Plan **独立HTTP** `/v1/coding_plan/search` 适配，复用有界HTTP重试，规范到原ProviderSource/ProviderSearchResult。它不调用MCP；接口依据来自本机已安装适配器源码，并以真实HTTP实测。
- v7配置：`RESEARCH_SEARCH_PROVIDER=tavily|minimax`，默认tavily；可选 `RESEARCH_SEARCH_FALLBACK`；MiniMax使用 `MINIMAX_SEARCH_API_KEY`、`MINIMAX_SEARCH_BASE_URL`（默认官方host）。产品不读取桌面MCP配置、不自动复用LLM密钥。验收脚本仅在隔离子进程内显式映射已有配置，不改密钥文件。
- 切换供应商也先扣同Run逻辑搜索预算，总计≤3；同查询传输重试仍受原配置约束，物理尝试另记。每个搜索事件记录实际provider；来源持久化使用真实返回provider，不再固定tavily。LLM≤3、正文尝试/来源≤5、累计提供≤100、交付并集≤40、单片≤200不变。
- 实际18080 API进程中Tavily/LLM配置均存在。使用该进程配置独立探针，本次Tavily返回HTTP432，分类request_rejected；**不能据此倒推历史两轮故障或未经依据解释为额度/认证/网络**。
- MiniMax独立HTTP返回200；Google官方域/路径及正文读取成功。但OneDrive/Dropbox多数返回本地化路径，初始被原政策拒绝；首个真实Google Run `36b10fee-d91e-410b-98be-f1d2d3b1dfef` 仍partial，不能把HTTP成功称质量通过。
- 后续新增语言版本**候选**：同HTTPS host，拒绝userinfo/非443/HTTP；Google保留edition参数只提议`hl=en`，locale路径只生成已有允许namespace的候选；不换host、不扩大白名单、不把译文当原文。保存discovered_url→read_candidate；每次读取照常扣预算、逐跳检查并使用实际正文快照。无法通过原政策的legacy host/path继续拒绝。

本机证据：`work/phase8-supervision/20261018-codex-search/` 的 `initial-audit.json`、`http-smoke*.json`、`provider-red/green.log`、`factory-red/green.log`。这些独立冒烟不计入原十题或另八题的质量分母。

## 两项有实际红例的契约修复

**Dropbox已读但漏提供：** 冻结原#2逐步回放得到552个catalog单元，旧入口谓词只允许第1步；前提和第2—8步均entry_conflict，并非预算耗尽或正文缺失。入口只在第一步出现，不必在每句重复。现在仅同快照/同flow_id的真实peer可提供入口语境；最终支持验证只用绑定peer，不能借未选择入口、兄弟流程或其他来源。平台/操作/条件/数量与完整步骤守卫继续独立执行。新增完整提供、兄弟列表拒绝、Windows拒绝三例。

**PDF object_unknown：** 原对象词典不能表达PDF files，且泛指files反而可吞掉格式限定。现在原问中“大写字母数字格式名＋file(s)”形成保留完整原文span的字面对象，canonical保留格式，既适用于PDF也适用于未见过的QXZ；候选files不能满足PDF files。没有产品能力/格式答案表，也不声称支持任意自然语言对象。源文仍需独立证明格式，TXT不能代替PDF。

`mask_verified_raw`新增字段改字、span错位、Raw愿望不能豁免另一字段承诺的测试，当前通过；它只验证proof机制，不替代新Requirement/PRD逐章语义审核。两个交接格式红项已整理。

## 工程与真实验收账本

以下均保留时间与代码适用范围，不借旧成绩补签：

| 检查 | 当前实测/边界 |
|---|---|
| 接手八核心基线 | 230 passed，修复前 |
| provider旧兼容＋新诊断 | 280 passed，搜索安全分类后 |
| factory/预算/管线 | 52 passed，locale候选前 |
| 同flow入口＋八核心 | 233 passed，locale候选前 |
| 新五专项 | 27 passed，含对象/proof/入口/预算，locale候选前 |
| 最终SQLite全量 | 1509 passed / 11 skipped；`sqlite-final2.log` |
| 最终全新临时PG全量 | 1520 passed；`codex-pg-full2.log` |
| 另一全新PG完整迁移往返 | upgrade head→check→downgrade base→upgrade head→check通过；`pg-launch2.log` / `codex-pg-migrations2.log` |
| Ruff | 全范围check通过，涉及14文件format通过；`ruff-final2.log`；收尾五专项35 passed，`close-scoped.log` |
| 前端 | 182 passed、lint与生产build通过；桌面1440和390px实页检查完成 |
| 原10及另8真实、同集复验 | 以下四套均同最终产品代码；原10每轮1 completed/9 partial，另8每轮全partial |
| 最新真实链路 | Research失败；测试PM→真实AI Requirement v2→reviewing/approved→真实AI PRD v4生成成功；不宣称Research全链成功 |
| 独立原文与模拟审批/Raw | 29URL独立读取成功；新目标取消/批准/重复SIM/归档409与项目隔离通过；Raw12行9字段不变 |
| 正式用户验收 | NOT MEASURED，不代签 |

浏览器采用本轮18081最新API，读取已核验data_directory/revision的自有e2e PG；原18080保留。生产Next13080新build明确指向18081。不得把旧18080进程与新构建混称同代码。

全量回归后只将新增测试中的lambda改为def以满足Ruff，受影响文件随后重跑、收尾五专项35通过；产品源码未再改变。保留这一测试文件整理时序，不把旧工程成绩或旧浏览器服务混入本轮。

## 同代码两轮真实Research与独立审核

四套冻结文件均位于`work/phase8-supervision/`，每套原始问题不改，保留原10分母；另8单列，不挑最好一轮。均为后端独立MiniMax HTTP搜索及真实MiniMax-M3，无搜索provider_failure；这只证明搜索传输可用。

| 题集 | 第一轮 | 同集复验 |
|---|---|---|
| 原10 | `20261018T092243Z-research-agent-v7-original/live-research.json`：1 completed/9 partial | `20261018T092848Z-research-agent-v7-original/live-research.json`：1 completed/9 partial |
| 另8 | `20261018T092244Z-research-agent-v7-extended/live-research.json`：8 partial | `20261018T092748Z-research-agent-v7-extended/live-research.json`：8 partial |

独立HTTP＋HTMLParser读取29个URL全部成功，审核36个Run、139条confirmed事实。严格字面辅助比对53项不一致，复查均可由列表编号、链接/按钮alt与空格标点差异解释；去这些格式后全部能找到字面序列。**没有据此声称零事实错误或语义PASS，也没有放松产品引文守卫**。主要失败是原文与原问支持关系及覆盖；逐Run账本：[质量摘要](evaluations/phase8-v7-search/quality-summary.json)。独立正文与原始辅助结果在本轮`independent-official-final/`保留。

| 原题 | 两轮独立结论（Codex复核，不代签用户） |
|---|---|
| #1 Google Photos多选/手动与自动 | 均有多选原句、手动步骤与自动定义，主体事实可用；对照/流程组织仍不稳定。重复本身不阻断 |
| #2 Dropbox iPhone多选及限制 | 均未交付可用手动事实与限制，未回答 |
| #3 OneDrive iPhone多选 | 首轮object_unknown、复验mode_replaced，正常请求规划失败 |
| #4 Apple三入口区分 | 两轮product_not_in_input，未回答 |
| #5 Drive多照片一次上传 | 两轮未验证；普通files步骤不替代多选能力证明 |
| #6 Google单张手动步骤/自动区分 | 两轮手动四步与自动定义可用，比较绑定仍partial；不因单纯重复升级问题 |
| #7 Dropbox关闭自动后手动添加 | 两轮交付的是camera uploads启用流程，原文真实但场景错误，主要质量FAIL |
| #8 OneDrive已有图片手动上传 | 两轮均未回答 |
| #9 Apple手机选项/共享图库 | 首轮完整：三选项、Photos App入口与版本原文核对通过；复验漏共享图库义务 |
| #10 Drive普通上传步骤 | 首轮未答，复验完整四步独立核对通过；按钮alt不作为主要问题 |

另8：中文Google步骤两轮分别平台契约/analysis_facets枚举失败；中文Dropbox两轮action_unknown；中文Apple两轮product_not_in_input；英文改写Drive两轮action_unknown；中文OneDrive两轮未验证。英文分析首轮有Google事实但Dropbox场景错误、无支持分析，复验协议失败；中文分析两轮规划/类型错误；离线题两轮都没有证明离线备份，复验还混入Locked Folder和离线隐私说明。分析能力仍未闭环，不能以partial标记掩盖错误方向的支持。

四套实测预算最大值：LLM3、搜索3、正文尝试5、来源5、累计提供100、交付引用并集25（上限40）；单片200守卫保留。未为切换provider重置预算。此前47号两轮仍是历史失败证据，不能与本轮当作稳定性对照或合并计分。

## 最新业务链、PRD逐章与页面

- 最新浏览器PDF Research Run `1b2289bd-2a53-4789-9442-b55763d066e1` 为partial_failed/object_unknown、0来源。独立诊断`3e5dc9e7-dcaa-45e8-be45-a0032e475d65`为partial/support_incomplete：模型小写`pdf files`仍失败，大写`PDF files`可规划。当前不是任意对象表达已解决，也不是Research→来源成功。
- 测试PM真实设置BV=5、评分3.89/P2、测试Accept；不代表三文鱼正式业务批准。真实AI Requirement `7df9e343-bb5d-4d43-bce2-4014059d27d7`，prompt=requirement-generation-v2，合法draft→reviewing→approved。
- 真实AI PRD `6ff6a5f0-7184-42d3-b5dc-dd35c264271c`，prompt=prd-generation-v4，版本1、draft、HTTP201；未accept，未伪造正式签收。全文：本轮`workflow-requirement.json`/`workflow-prd.json`。

| PRD章节/字段 | 支持复核 |
|---|---|
| background/problem | 材料化F004/F005完整原话及2条/2非空去重用户样本，未推断原因、发生率或总体规模 |
| target_user/user_story/product_goal | 明确提案、待验证；围绕上传失败/体积方向；未当作已实现能力或实测收益 |
| proposed_solution/functional_requirements/user_flow | 状态、前置体积提示、原因调查、重试均属建议；20MB是用户报告观察起点，未宣称已证实服务阈值 |
| edge_cases | 六类为拟议设计场景，invalid_input/empty_state的“来源F005”只能说明同模块背景，不能证明真实发生；保留为草稿人工修订项，不冒充Raw事实 |
| acceptance_criteria | 可验证标准，note明确尚未执行；不当作这些PDF业务功能已实现/验收 |
| success_metrics | 仅基线/指标定义，未设虚构收益或量化目标 |
| risks | 两条known为F004/F005原话；原因假设单列speculative；mitigation为待验证建议 |
| mvp_scope/evidence_sources | 提议范围和排除项分开，证据只支持用户报告，不泛化商业结论 |

本链内部Raw事实/known风险主要问题已改善，但Research失败意味着**不能把Research→PRD整体标PASS**；旧照片PRD失败和旧SIM继续留档。重复“提案/待验证”、重复说明和草稿措辞按最新要求放过，不再扩修。

生产Next13080→新API18081，真实桌面1440及390px检查Requirement/PRD/Insight；390px document宽=scrollWidth=390、Requirement/PRD主区域无越界。已查看桌面Requirement、390px PRD及最终桌面PRD截图；项目切换后Requirement表单/SIM被清空，PRD显示不存在，Insight不展示Research内容。截图见`output/playwright/phase8-v7/codex-search-*`；本轮页面尾段无新增console错误，跨项目404网络错误是预期拦截。

## 新目标模拟审批、隔离与保护

在上述新Requirement上，浏览器Prepare→Cancel，第二次Prepare→Approve；数据库核验取消0目标、批准1目标；API重复Approve同一个SIM且仍1目标。新Prepare后合法approved→archived，再Approve409、0目标。Requirement因此已归档，PRD仍是draft；不复用旧目标或恢复旧审批脚本。Run/事件详见本轮`tail-tool-isolation.json`及浏览器日志。

跨项目B访问新Requirement读/写/生成PRD/PRD历史、PRD读/写/接受、Tool历史/Prepare/读/批准/取消及Research读，共13种请求均404；跨项目写请求没有改动目标。页面切换验证与API证据分别保留。

Raw最终12行、9字段逐字段等于原e2e快照，SHA256 `bf8ff4e186f47965f7ff1c99d845c145ab761f7cd4ce2ec72f3063deaf03fecd`。本轮237文件基线仅7个预期源码/测试改变，无文件丢失；完整保护审计见本轮`final-protection.json`。旧测试/Prompt、0001—0015、密钥、两ZIP、AGENTS及导入CSV保留；CLAUDE.md现场不存在，未新建。原API18080/launcher/临时PG及新API18081保留，未写server-stop，DSN未打印未提交。

## 本轮结论与复测入口

工程检查PASS、后端替代HTTP搜索可用；测试PM→Requirement→PRD和模拟审批执行通过。**Research正常英文/中文/分析/对象表达仍有主要功能限制，质量FAIL，Phase8 PARTIAL。** 正式用户验收NOT MEASURED；SIM仅数据库模拟，真实外部集成DEFERRED。历史Phase2语义Embedding、Phase3/4/6/7质量和人工复核限制保留。

复测使用本轮四套冻结题集及同问题，不重新导入12条Raw；服务配置只核对存在性，独立HTTP结果与MCP能力分开。浏览器可读当前PRD与归档需求；若再测审批应另建合法测试目标，当前归档目标Approve应409。优先待解决仅限正常Research规划、正文相关流程/条件支持和支持分析；不再为小文案扩范围或启动第三轮全题集。未满足提交门槛，index空、HEAD未变，无commit/push；本轮到此停止，不进入Phase9。

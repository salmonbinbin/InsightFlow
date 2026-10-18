> **后续只读根因补充（2026-10-17）：** 最新定位见[43号复盘](43-Phase8深度根因复盘与新对话交接.md)。特别是原#9，第三轮已选中关键手机选项和Shared Library入口，最终被best按7条对6条confirmed选择回第二轮而丢失；不能继续粗归因为没有取得/选出原文。本报告的实测数字、质量结论和失败交付不变；后续分析未改产品代码或重新真实调用。

# Phase8业务补齐实施与独立验收报告（v6）

日期：2026-10-17。Codex负责范围、架构、测试、官方原文独立复核与Git；Claude按允许文件实现。**最终结论：Phase8 PARTIAL，Research真实质量 FAIL，未验收、未提交、未推送；STOP，不进入Phase9。** 本报告取代40/41号文件中的“下一轮待执行”作为当前实施状态；36号根因更正有效，39号是v5历史结果，均不抹除。

## 1. 现场基线与范围

接手及交付HEAD均为`449e8790b8da8b9f04f4e3739cd9740606241cc4`，分支`codex/phase6-evaluation`，暂存区为空。保留接手时大量未提交工作，未reset/clean/全仓暂存。最初产品指纹与上一轮归档相符；Codex现场重跑基线SQLite1092/11skip、隔离PG1103/完整迁移往返、前端154及lint/build，再回放失败资料，没有把旧报告或Worker完成声明当作本轮证据。

用户两套PG仅尝试只读核对，均Operation not permitted，revision/行数为NOT MEASURED；未改权限、未重启、未降级。8000归属于其他项目，未干预。本轮真实链路全部使用自建临时PG，迁移至0015、FastAPI18080和生产Next13080。配置现场存在，真实Tavily/MiniMax调用已发生；key/.env按JSON读取，未source、未输出值。最初核对见[现场审计](evaluations/phase8-v6/site-audit.json)。

批次按失败夹具→任务契约→正文/取证/协议→事实与分析→前端→真实链路及最终验收执行。范围只在Phase8；没有改Phase5/6核心生成逻辑或将Research混入Internal Evidence。完整架构取舍见[架构审查](evaluations/phase8-v6/architecture-review.json)。

## 2. 已实施的业务变化及边界

1. 活跃Research改用v6。服务器从原问题生成带原始offset的子句锚点，模型仅提供任务、证据ID、绑定及分析候选，禁止自评完整性。产品、平台、入口、数量、条件及未知子句由任务契约保留；完成仍由独立证据谓词判断。模型不是完整性裁判，但当前候选契约仍过脆，不能称通用语义覆盖已解决。
2. 结构化引用使用ID与offset/length在本Run官方快照中逐字提取；保留原始双引号，例如Dropbox第三步`Tap the "+"`。不靠反转义、模糊匹配或删步骤过关；旧v14引文外扩写/否定、数量、名称、来源ID/URL拒绝测试保留。成功官方正文缺失的v6事实即使provider snippet有字也降为未验证。
3. 正文句子定位保留域名/版本号前缀，嵌套选项、条件、目的地进入证据单元。覆盖检查补齐完整编号库存、Choose Existing后续动作、多选数量、手动/自动证明及条件。删除空site查询，正文/来源去重、补搜缺口及停止原因入trace。事实/覆盖等价时才用合法分析数量作候选tie-break，不用分析提高完成判定。
4. `research_claims.py`将事实与推断/建议分开。事实须严格引用；推断/建议永远not_verified、无直接source_refs，以真实Finding UUID指向同Run/同项目confirmed事实，市场模式须至少两产品并限定样本。新增0015可空字段`claim_kind / supporting_finding_ids / evidence_location`，旧行NULL兼容。活跃v6可输出竞品分析、市场模式、机会、风险；旧v5兼容函数仍保留历史固定数组，不能误读为活跃v6也固定为空。
5. Prompt v6与结构化schema不再要求模型summary；服务器生成明确标注的内部上下文。任务合约失败与事实/分析候选独立验证；契约错不吞掉全部合法事实，也不允许坏任务完成。协议失败、provider/transport、取证、证据不足与覆盖失败分开保存，不把它们写成“官方没有资料”。仍存在缺工具块和未知证据ID，未宣称协议稳定。
6. 前端展示事实/推断/建议、逐项覆盖、真实分类空缺原因、分析→支持事实→官方来源，项目/Run切换清理展开状态。所有回答继续显示“完整性未验证”，推断/建议继续待人工复核，没有Mock研究数据补位。

预算未提高：每Run逻辑搜索≤3、正文/来源≤5、唯一提供证据单元≤100、每次选择≤40、总LLM≤3；本轮规划与候选同一次调用，无额外规划调用。正文白名单和每跳安全检查保留，3跳/10秒/解压4MiB/文本24000字符。最终18题trace预算均未越界；attempt_count包含搜索与模型，不能把它当作LLM次数。

## 3. 最终工程验证

最后源码冻结为[final4产品指纹](evaluations/phase8-v6/final4-code-manifest.json)，后续只写文档/证据。日志退出码均已收取为0；没有以旧轮数字替代最终轮。

| 检查 | 最终结果 | 证据 |
|---|---|---|
| SQLite全量 | **1194 passed / 11 skipped** | [日志](evaluations/phase8-v6/final4-sqlite.log) |
| 全新隔离PG全量 | **1205 passed** | [日志](evaluations/phase8-v6/final4-pg.log) |
| 完整迁移往返 | upgrade/check→downgrade base→upgrade/check，含0015，POSTGRES_VERIFICATION_PASSED | 同上，仅全新临时库 |
| Ruff | PASS | [日志](evaluations/phase8-v6/final4-ruff.log) |
| 前端测试 | **157 passed，15文件** | [日志](evaluations/phase8-v6/final4-frontend-test.log) |
| 前端lint | PASS | [日志](evaluations/phase8-v6/final4-frontend-lint.log) |
| 生产build | PASS | [日志](evaluations/phase8-v6/final2-frontend-build.log)，最后前端源码修改后构建；其后只改后端，生产Next使用此构建 |

SQLite的11个PG专用跳过不计入通过；PG全量执行这些检查。独立失败→修复日志保留，不删除失败轮。最后两处管线修复独立14项通过，见[失败](evaluations/phase8-v6/analysis-tie-red.log)与[通过](evaluations/phase8-v6/analysis-tie-green.log)。绿色回归不证明真实语义质量。

## 4. 最终原十题：保持原分母

最终真实Tavily＋MiniMax-M3，Prompt research-agent-v6，归档原始运行`20261017T133502Z-v6-original`。原QUESTIONS逐条与v5冻结归档一致。程序**4 completed / 6 partial_failed**；独立官方原文核对为**4完整 / 3局部 / 3未验证**。这是Codex独立技术核对，不代签三文鱼验收。

| 原题 | Run | 程序 | 官方原文独立判断 |
|---|---|---|---|
| 1 | `b62ce4dc-3cce-469f-8d4a-78b154c5cb1c` | partial_failed | 局部：多选Tips有确认事实，但手动/自动区别及任务约束失败；官方有答案，程序未闭合。 |
| 2 | `233d3fb5-5925-4b4a-ae82-9f6cc5ae3c67` | partial_failed | 未验证：0条确认事实。完整手动八步已选入但逐句数量门全部降级；限制子题未覆盖。普通复数不自动证明严格多选。 |
| 3 | `e5e67351-6073-4656-a314-b6fa61d15329` | partial_failed | 未验证：0条确认事实。已有完整手动/Choose Existing正文，逐句数量门全部降级及unknown_evidence_id；不能说官方无答案。 |
| 4 | `4cb23f68-2a77-4500-bf18-f2cf364fa5d1` | partial_failed | 局部：确认Shared Library多选局部事实；iCloud同步、网页上传及共享图库三入口比较未完成。 |
| 5 | `68166bbb-152f-4d9f-b2dc-ebb96c2e8f32` | partial_failed | 未验证：0条确认事实，普通上传四步已取得；不据普通复数推断明确多选，另有任务类型/约束失败。 |
| 6 | `029fcc6e-e414-45b9-9868-91a344a5f7fb` | completed | 完整：手动四步、替代按钮、单图存储条件及自动备份独立依据齐全；多选Tips是额外背景。 |
| 7 | `475d22a6-b702-4507-ab38-c4f85056a7c7` | completed | 完整：禁用camera uploads仍可手动添加的条件句及八步齐全；第三步内部双引号保留。 |
| 8 | `9c5d15eb-2002-42e4-a661-15936a81b1b2` | completed | 完整：Add→Upload→Choose Existing→Photos→浏览/选择→Done齐全；另有非所问Take a Photo分支和导航噪声。 |
| 9 | `9d68d675-4dcc-4719-ad15-d835a84b6ab3` | partial_failed | 局部：仅确认Shared Library既有内容移动事实；未给出iCloud.com手机三选项及上传共享图库应使用Photos App的关键注释。 |
| 10 | `1bca3bb8-abfc-4101-97bf-80cd66bb727c` | completed | 完整：Android四步及My Drive去向齐全；未把普通复数扩成严格多选。 |

44条confirmed均核对独立重取官方正文，未发现捏造事实；其中42条辅助字面审计相符、2条Google图片alt与标点空格有表示差异，人工确认实际文字一致，没有放宽生产引用。4条completed未发现整题完整性误阳性。明确相关性误收**3条**：#8拍新照片分支两句和HEIF导航一句，与选择已有图片无关；这不等于三条假来源。其余背景/冗余另作噪声，未算作整题答案。

#2/#3/#5均0条confirmed，包含官方已取得的普通步骤；#3尤其是任务/证据ID和逐句数量门使手动流程不可用，不是官方没答案。#9官方页面明文列出`Photo Library / Take Photo or Video / Choose Files`以及Shared Library应使用Photos App及版本条件，但本Run没有交付，属程序取证/选证/覆盖失败。保留严格数量门不等于已经完成多选需求。

完整原问题、实际query、来源、官方快照/定位、协议metadata与失败断言见[原十题轨迹](evaluations/phase8-v6/original/live-research.json)、[逐题语义核对](evaluations/phase8-v6/original/semantic-review.json)、[独立官方页面](evaluations/phase8-v6/original/independent-official-review/review.json)、[失败包](evaluations/phase8-v6/failure-pack.json)。

## 5. 中文、改写和分析八题：独立分母

最终新增8题程序**2 completed / 6 partial_failed**，独立核对**4完整 / 3局部 / 1未验证**。中文Apple和Drive改写实际确认事实已能回答，却被任务/覆盖程序保守漏判；不将它们偷换成程序完成。与原十题分开统计。

| 新增题 | Run | 程序 | 官方原文独立判断 |
|---|---|---|---|
| 1 | `697fc7b2-411c-48fb-8cfb-e35bda7a035d` | completed | 完整：中文单张手动四步与自动依据齐全，程序completed。 |
| 2 | `2d8a5b32-bbc8-4428-b927-f7401b030885` | partial_failed | 局部：只取camera uploads启用、overnight等相邻事实，未回答禁用后手动八步；官方已存在完整目标答案。 |
| 3 | `c12e6a8f-7612-4ae8-9b92-3143157b2f28` | partial_failed | 完整：确认手机三选项、Photos App入口及该页面iOS16.1/iPadOS16/macOS13版本注释；程序task_subject失败造成漏判。 |
| 4 | `4025a30f-d4b0-402d-9137-7e2c27ce968e` | partial_failed | 完整：确认Android上传四步及My Drive去向；程序冗余任务/未解析子句造成漏判，附带其他节噪声。 |
| 5 | `19cf387c-dba4-479b-9e2d-0cab0807b771` | completed | 完整：中文已有图片完整步骤及iCloud优化限制齐全。 |
| 6 | `7cb9a706-5a42-46a4-9449-1cb0f633ee47` | partial_failed | 局部：两产品事实、3条分析候选已持久化：竞品推断/样本模式/机会建议；风险缺项，相对优缺点不足，另有协议/任务失败和相关性误收。 |
| 7 | `d4c46f30-ba32-4088-a838-6acb97e13288` | partial_failed | 局部：Dropbox手动步骤有事实；Google选入动画/创建内容步骤，任务与协议失败，无实际分析。 |
| 8 | `c98555f6-9877-42d4-980d-f1e1b5694058` | partial_failed | 未验证：通用联网备份事实不证明离线条件；整体未验证，未宣称可离线上传。官方正文另有Good internet connection，条件尚未处理。 |

125条confirmed中117条辅助字面审计相符、8条为图片alt标点空格差异，人工核对实际文字一致；未发现捏造原文事实。completed两题无完整性误阳性。明确相关性误收**23条**（保守只计已在逐条账本列明的错误功能/平台/流程，不把所有冗余都算误收）：中文Dropbox把相机启用、overnight及OneDrive套餐内容代替目标手动流程；英文分析收Windows视频限制；中文分析收Google动画/创建步骤。每条引文、原因和来源均见[新增题语义账本](evaluations/phase8-v6/extended/semantic-review.json)。

英文分析实际产出竞品推断、样本模式与机会建议共3条，风险仍缺。中文分析没有分析产物，且最终协议失败。offline题保留条件且未完成，但confirmed通用步骤不能证明离线能力；不能因此宣称离线可上传或官方完全无资料。

最终18题每题最多3次LLM：原十题无工具协议失败，仍有多次任务约束/subject/action/kind失败及unknown_evidence_id；新增英文分析第1次stop_reason=tool_use但content仅text，没有tool_use块，中文分析第3次协议失败。缺工具块即协议失败，不解析thinking或从任意文字拼JSON。已取得官方正文的程序覆盖失败与真证据不足分别记录。[新增轨迹](evaluations/phase8-v6/extended/live-research.json)。

final3与final4代码不同：原十题程序2/8→4/6，新增0/8→2/6。中间轮曾出现只一句能力陈述就completed、provider截断Tips被确认等问题，已形成负例修复；这些失败保留在本机work，没有拿最好一轮抹去。**未在同一最终代码完整复验同集，不声称稳定。**

## 6. 真实生产浏览器的分析与隔离

最终代码另通过生产Next实际提交英文分析，Run`4b32e6ba-3f01-4c86-b933-a82a0f542ca1`，partial_failed。真实保存34条confirmed事实、5条分析：竞品推断2、市场模式1、机会建议1、风险推断1。分析均not_verified且无直接source_refs，支持UUID均为同Run的confirmed事实；实际展开风险支持事实并看到Google/Dropbox来源链接。[API证据](evaluations/phase8-v6/browser-final4-analysis-evidence.json)、[展开截图](evaluations/phase8-v6/browser-final4-support.png)、[链接](evaluations/phase8-v6/browser-final4-support-links.log)。

这是业务分类恢复的真实证据，**分析质量仍FAIL**。页面仍有相邻条件和重复overnight片段，展示排序/去重尚有改进空间，不能声称所有摘录均已归并成完整答案：

- 优缺点分析将Windows视频限制作为移动端照片流程缺点，范围错误。
- 竞品分析标题把Google多项touch-and-hold分支写为per-item，标题/正文容易误导。
- 市场模式引用的6条支持事实没有包含Google自动备份陈述；该陈述虽在官方页存在，当前绑定不足。同Run/项目、支持ID存在是必要条件，不能代替语义支持。

以上不是已确认官方声明，UI仍清楚标推断/待核对，不能因而免除分析质量要求。完整核对见[浏览器分析语义审计](evaluations/phase8-v6/browser-analysis-semantic-review.json)。

项目切B实测清除A研究/来源/事实，显示“该洞察不属于当前项目”。Research Run/sources/findings/history及Tool读取/取消/批准/Prepare跨项目请求404。最终同代码再次切到B，Research区域0个、无Dropbox来源和上次Run残留，新Run及其sources/findings/history用B项目参数4项404，见[最终浏览器隔离](evaluations/phase8-v6/browser-final4-project-b-audit.log)和[最终API隔离](evaluations/phase8-v6/workflow-final4-cross-project.json)。显式project_id隔离通过，不冒称身份认证/完整租户安全审计。[项目切换](evaluations/phase8-v6/browser-project-b-final.log)、[20项HTTP及DB审计](evaluations/phase8-v6/final-tool-api-db-audit.json)。

## 7. Research后真实核心链路与PRD质量

初次隔离链路实际完成CSV12条→MiniMax分类12/12→local-hash-v1聚类→真实Insight→浏览器Research/来源→测试场景BV=5及PM Accept P2→真实AI Requirement→真实AI PRD。没有预造approved Requirement替代。初次Requirement首调422空risk evidence ID，第二次201；PRD首调503、第二次201，均保留失败；PRD为draft、2项speculative_risk警告。原Requirement随后用于模拟批准/归档，因此该动作不等于三文鱼正式产品批准。ID见[原链路账本](evaluations/phase8-v6/workflow-ids.json)。

最终final4又从同一真实分类/聚类输入生成新InsightRun`849d369c-30e1-4c45-af68-a4c8a6ea2982`，新Insight`0f7d38fb-633d-4258-a56b-c0b23ba800a6`。生产浏览器Research Run`8c89b069-ef72-471d-890d-da1932528ce4` completed，展开来源后BV=5、PM Accept P2，真实生成Requirement`0357e0b7-6163-4278-8d2a-c4050d62050e`（draft、MiniMax-M3）。

最终PRD首次真实503（tool input缺14个必填字段），明确重试后真实201，PRD`e3455200-4c9b-4280-9ac8-516eb7f34604`（draft、MiniMax-M3、prd-generation-v3、1项speculative_risk警告）。页面真实展示15个content字段（业务13章及evidence_sources/evidence_ids）。引用集合仅F001/F002/F003，proposed_solution.is_proposed=true，指标均baseline_only；没有凭空量化收益。链路**执行通过，但首次失败保留，不声称稳定**。[最终链路ID](evaluations/phase8-v6/workflow-final4-ids.json)、[Requirement](evaluations/phase8-v6/workflow-final4-requirement.json)、[PRD](evaluations/phase8-v6/workflow-final4-prd.json)、[首次失败](evaluations/phase8-v6/browser-final4-chain-prd.log)、[重试成功](evaluations/phase8-v6/browser-final4-chain-prd-retry.log)、[页面](evaluations/phase8-v6/browser-final4-chain-prd.png)。

本轮自动化还曾两次等待了错误的复数/endpoint条件而超时：BV与Requirement实际已分别200/201且页面已更新，独立API/日志确认后继续，没有重复创建Requirement。该工具等待失误与真实模型503分别记录，不混为模型重试。

核心链路执行与生成内容质量分开：聚类仍local-hash-v1，不能关闭真实语义Embedding限制；新Requirement把“单选可能源于审核/后端策略”标known并引用F001/F003，但这两条Raw只证明单选问题，没有证明原因，是上游草稿语义风险，不能声称完整PRD业务质量PASS。仅隔离测试场景的PM操作，不代签真实用户接受。

## 8. 模拟批准、Raw与保护文件

真实AI生成Requirement按合法draft→reviewing→approved后，Prepare→Cancel不生成目标；再Prepare→Approve→重复Approve同一SIM且只一条SimulatedIssue；新Prepare后合法approved→archived，Approve409且无新增目标。跨项目隔离一并检查。**SIM仅数据库模拟，真实Jira/Notion/飞书/MCP继续DEFERRED。**

Raw12行、9字段按ID对齐全量比较。TIMESTAMPTZ仅统一UTC ISO比较同一时刻，其他字段逐字；规范化前后SHA256同为`782d1695f41eb2e5c4a1deb832a6f23fda99a64fde3c7b0ee94e4c35e67ec2d0`。初次字符串比较因Asia/Shanghai与UTC格式差异失败，诊断保存，没有删列/改Raw蒙混。最终二次链路后再次核对相同，见[最终状态](evaluations/phase8-v6/final-state-audit.json)。

71项保护指纹中，仅两份旧Research测试的活跃v6版本/模块声明有预期变化；未降引用负例或删断言。ZIP两份、key、AGENTS、Raw模型、旧迁移0001—0014和旧Prompt v1—v5均未变。用户库未写入，所有破坏性迁移仅全新临时PG。

## 9. Worker与过程审计

[15个job账本](evaluations/phase8-v6/jobs.json)及[工具/写入审计](evaluations/phase8-v6/worker-audit.json)保存原threadId、允许文件、完成/超时/无实施情况。最后两个job为`74c5670abe6a4f81931faec3cf6c2a85`与`d7d73d0ca37f4e9a988cae6732989573`，均已完成并独立测试；当前无活动Worker。Worker没有执行Git、测试或权限操作。

过程有两项偏差，不能写“零违规”：早期job排队后Codex曾复制一份不重叠独立测试，违反Worker期间不写源码/测试的顺序约束，已记账并修正；3个job合计4次请求Bash被工具层拒绝，没有实际执行。所有实际Edit/Write路径均在各job允许文件内。部分job超时或声称完成但无代码，均以实际diff/测试判断，未据声明验收。

## 10. Gate、剩余阻断与STOP

| 门槛 | 结果 |
|---|---|
| 最终工程/迁移/前端 | PASS，数字见第3节 |
| Research分类/事实支持持久化和UI能力 | 已实施，真实输出可见；业务整体PARTIAL |
| 原题与中文/改写/分析真实质量 | **FAIL**，存在已知答案未交付、任务脆弱、相关性及分析支持缺口 |
| 真实链路执行 | 见第7节，执行不代表内容质量通过 |
| 模拟审批/Raw/显式项目隔离 | PASS；真实外部集成DEFERRED |
| 同最终代码整集稳定复验 | NOT MEASURED，不声称稳定 |
| 用户数据库revision/行数 | NOT MEASURED，只读权限失败，未绕过 |
| 三文鱼正式验收 | 未签，不代签 |

主要阻断是任务候选契约与问题语义的对应仍过脆；逐句数量门降级合法辅助步骤；#9取证/选证未覆盖；中文流程检索误选；分析只校验支持ID、尚不能证明范围及语义支持。后续如另行授权本阶段返工，先用归档的Run、query、协议元数据、官方原文和失败断言重放，续原Research线程，不能靠扩大关键词/原题答案表、放松引用或全面拒绝来过关。

Phase2真实语义Embedding、Phase3人工语义复核、Phase4正式业务评分、Phase6 Artifact/长期质量、Phase7稳定性与人工验收等历史限制全部保留；本轮局部链路不追溯补签。文档、人工复测流程与任务账本已更新；[脱敏检查](evaluations/phase8-v6/sanitization.json)未匹配已知密钥值，未归档配置或临时库URI。[SHA256清单](evaluations/phase8-v6/manifest.json)覆盖归档文件。两份旧测试仅module/version声明变化已反向替换复原原始SHA256证明，见[测试保护核对](evaluations/phase8-v6/protected-test-diff-verification.json)。

本轮浏览器、FastAPI18080、生产Next13080均关闭；停止自建PG前以data_directory、postmaster.pid与进程命令三者核对所有权，pg_ctl正常停止。8000其他项目及用户PG未停止。[服务清理记录](evaluations/phase8-v6/service-cleanup.json)。

**未达到全部提交门槛，因此不暂存、不提交、不push。Phase8失败交付，STOP，不进入Phase9。**

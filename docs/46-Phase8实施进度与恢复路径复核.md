# Phase8 v7实施进度与恢复路径复核

2026-10-18；Codex Supervisor。**工作报告，不是最终验收或交付。Phase8继续PARTIAL，未暂存、未提交、未push。** 45号为主计划，本文记录现场实施中经失败证据确认的细化，不覆盖43/44约束。

已实施稳定QuestionSpec、独立逐字事实、来源/任务适用、同流程数量/条件/版本、支持账本、类型化比较与分析、抽取式回答、v6/v7显式路由、新AI Requirement v2/PRD v4已知风险原文守卫及正式页面版本选择。旧Prompt和迁移0001—0015未改，旧测试未改。前端原线程先实现后两项新测试及生产构建失败，已附证据续接修复；之后177项前端测试及生产构建通过。所有轮次数字仍属中间结果，最终代码须重跑。

四个冻结官方正例已独立通过：Google手动流程、Dropbox禁用相机上传后的完整八步、OneDrive已有图片分支及Apple手机选项。单元片段保持≤200字符，长句相邻片段和完整分支不能截去条件；模型漏选同流程末步可由结构闭包补入**已提供**的精确单元，补入项记账，整体仍≤40；污染绑定不因闭包过滤变完整。引文不改写、不反转义。upload/add/backup仅作为远端导入操作族检查关联，原文功能名称、手动/自动、对象、平台、入口、数量和条件继续分别验证；sync/move/delete不因此等价。比较从两侧已验证的要求关系推导，不再靠摘录数或两家来源名字。分析每个前提必须绑定完整支持组，五类产物各自验证，模型不能签完整性。

原十题仍保留分母，另八题独立记录。首轮v7十题程序0 completed/10 partial_failed；后续轮次有真实正例，但仍有规划/协议/选证失败，不能称质量PASS。原文HTTP包含检查与语义支持分开；未找到的文本必须复查，不能自动当引文捏造或全部忽略。各轮路径见work/phase8-supervision/20261017-implement-v7/日志和按时间命名的research-agent-v7目录，暂未作为最终公开证据发布。

## 规划失败后的预算恢复细化（实施前决定）

最新同代码预验显示：第1、2次都缺工具块，第3次虽得到合法规划，却无剩余调用可选证。此前设计只能记录没有回答，这是实际执行缺口。不能加第4次调用、解析thinking或任意text JSON。

保留正常的“规划→取证/选证→纠错”路径。仅在规划调用已失败、还没有有效冻结契约时，下一次转为**联合规划/选证恢复**：先依据原问题和同一官方白名单有界取证，给模型原问题锚点与精确证据；模型一次返回规划候选和选证候选。服务器先检查原问题/政策/数量/条件/产物义务并冻结首个有效契约，再用原事实、作用域、完整流程、数量、否定、支持和分析守卫验证选证。后续调用不能重新改写有效契约。联合响应的事实与分析仍分层验证，坏分析不得授权输出，也不能抹掉独立合法事实。

代价是恢复路径的规划会看到来源，因此必须明确禁止把来源中有答案的部分改成用户需求；原文锚点、未解析义务、原问题数量、条件及所求分析facet检查保留，最终继续独立原文复核。这个路径不证明开放语义理解正确。

预算不变：实际LLM含失败≤3、逻辑搜索≤3、正文尝试/来源≤5、累计提供≤100、最终选择≤40；所有资源调用前扣账，恢复不另开Run藏重试。源码范围仅新增research_joint_v7.py，调整research_pipeline_v7.py及本轮新建pipeline测试；API版本、数据库、旧Prompt、旧测试不变。须补“前两次协议失败、第三次联合恢复可产生真实事实”与“全部失败仍≤3且无捏造事实”的正反例。

## 尚未关闭的门槛

- 恢复路径的工程和真实调用复验，以及最终同代码重复原十题/另八题并公开差异。
- 独立官方原文与逐题完整/局部/未验证及误阳性审核；真实两产品分析完整支持。
- 当前新临时PG中的生产浏览器Research→查看来源→测试PM→真实AI Requirement/PRD与全章内容复核。真实分类12条、聚类和Insight已完成；Research已有真实完成正例，不代表后续门槛通过。
- 模拟审批/取消/重复批准/撤销状态、跨项目404、Raw九字段及所有保护指纹审计。
- 最终全量SQLite/新PG往返/Ruff及前端lint/test/build、文档/脱敏/任务账本。

只有全部前置门槛满足才精确暂存和本地提交；否则不提交，保留失败证据。不得代签三文鱼正式验收，不进入Phase9。历史Phase2/3/4/6/7限制继续保留。

兼容展示的精确补充范围：frontend/src/app/prds/[id]/page.tsx仅为历史/人工编辑known风险增加“未进行语义复核，回查原始反馈”提示；Requirement同类提示和Research“官方原文”用词一并修正，不改PM编辑/审批行为。

## 独立审查后的支持关系修正（2026-10-18，仍未最终验收）

本轮新鲜只读review确认平台替换、多产品借证、比较缺模式侧、数量跨动作、条件跨兄弟分支、空分析标签、坏候选占槽和版本区间误判八项问题。五个新增反例先红；首轮Worker虽然修复了反例，却使数量正例和手动/自动比较正例回归失败，已附日志续原线程修复。随后75项专项通过，但还不能以此宣称通用语义质量通过。

下一批从支持关系职责修正：每个产品、每个模式、每个条件/版本必须各有完整支持，不能只证明来源存在；上下界求交不能把范围当矛盾或仅满足一个边界。分析facet必须有对应的实际推导内容，不以标签数量算完整；优势/劣势仅能基于同维度完整编号流程提出操作环节的假设，显示产品、步骤数和差额，不能证明耗时或体验；风险/机会须有明确待验证提案，开放维度仍缺口。纠错候选不得被先前已拒绝的候选永久占槽。模型引用支持组应由服务器的已验证关系提供，避免重复抄写EID制造不一致。仍须独立原文审查，不视为开放语义判定器。

真实临时PG链路已走通分类12条→聚类→Insight→真实Research→测试PM→真实AI Requirement v2→测试reviewing/approved→真实AI PRD v4。首次Requirement503失败保留，第二次真实生成成功；不能只报成功次数。PRD逐章审查发现问题段把反馈外的完整选择器当前流程写成事实，提案边界也需更明确，尚未据此给内容质量PASS。模拟取消、重新批准、重复同SIM、撤销状态409、跨项目Research/Tool404及Raw12行9字段冻结一致已有本轮证据。正式验收未签。

### 输入义务与流程成员的实施细化

第二次只读审查确认 action/object/mode/kind/entry/漏产品六个原问弱化反例，均可在正确来源和正确平台下误报完整。这不是补题目词表的问题：候选拥有整anchor不能代表保留了anchor全部要求。新增服务器输入义务账本，保留各维度原文span、分支/产品归属、规范值和所求产物；未知/归属歧义保持缺口，其他清楚分支仍可交付。最终覆盖以原问义务为分母；候选只提供路径，不定义自己的验收标准。明确产品×模式不能通过子集通过。共享语法只规范产品无关的动作/对象/平台、屈折和中英文映射，不存原题答案或产品能力；仍不声称开放语义证明。

流程角色从句中任意动词改为结构归属：同快照、同流程、同平台/分支的编号flow_member属于最近操作标题所拥有的流程，可保留查找结果/时间条件步骤；不能借此证明句内另一个动作。真正Move/Delete等子标题仍首先隔离。多产品原子要求需分产品完整验证再组合，不能一条不同来源的真句就补齐第二产品。此批允许研究support/spec及新建research_input_v7模块（若职责分离需要），新测试只增本轮supervisor文件，旧Prompt和迁移不变。预算仍3/3/5/100/40/200。

### 输入义务剩余缺陷与下一批边界

P批次sync_timeout退出，确认子进程结束；部分修改保留，独立复跑89通过/10失败。新增未知canonical、app入口、单anchor动作对象交叉配对和分anchor HOW反例已红。后续输入语法与spec强制映射分两批；未识别值不免检，多动作对象必须按原文局部分支配对检查，而非分别做集合成员。候选遗漏任何明确原问义务都保留unresolved；有歧义不推断产品能力。分析与恢复专项另有7项失败，不能用旧绿色成绩覆盖。

Requirement/PRD项目上下文补丁范围：后端八资源路由可选project_id，在任何读写/模型调用前按资源归属拒绝404，旧无参数兼容；正式三详情页面始终传当前项目，PRD核对后才setState/读取历史。仅项目作用域，不引入身份系统；不改数据库。事实材料化仅新AI v2/v4，先完整候选校验和known原文守卫，再按实际引用子集材料化、最终重parse。去重反馈ID、非空用户ID；冲突原文或超限明确失败，不能截Raw。标签不能证明其它章节语义，最终仍真实逐章审核。

独立读审建议及待验用例：产品分支先于动作对象配对；共享动作/对象只按明确语法展开，不建立任意笛卡尔积。`upload files + delete photos`不能被`upload photos + delete files`满足，`manual upload files + automatic delete photos`不能交换模式。新增r-spec-red记录15失败/53通过；项目上下文红例实际八路由全部未返回404，5项兼容/未知资源通过（r-project-red）。这些是中间失败证据，不是最终Gate。

材料化验收还必须覆盖：只引F001时不能偷带bundle中F002/F003；PRD background和problem分别保留各自引用集合；重复反馈ID去重而不同ID同正文各计；同ID冲突Raw拒绝；空用户ID单列未知。helper和真实_call边界均验证超长Raw不截断、错误映射不泄漏正文。保留坏known先拒绝、缺必填章节先拒绝、旧v1/v3与PM路径原样的兼容测试。其它提案章即使含“提案”仍须独立内容审核，不能用前缀证明全文支持。

R批次超时退出，确认进程结束；独立复跑99通过/9失败。原问义务修复出现合法对象短语、mixed比较及平台显示enum回归；另外产品名内部Photos、中文第二动作和共享产品对角覆盖已独立新增反例并红，不能凭新增tuple字段即宣称完成。S批次按动作位置、产品保护span、逐owner义务和输出/事实职责分离修复；不增加LLM调用。

S批次同样超时退出，进程结束。独立复跑110通过/2失败（select+backup输入同义操作族、原子多产品owner匹配）。另新增动作位置/按动作绑定的显式mode反例仍红，T按原文位置建组，不能继续扩连接词词典。Worker日志仅解析工具名称和is_error等固定元数据，不读thinking/stream正文：R有3次Bash尝试均返回错误，S有1次Bash尝试返回错误；未执行。S实际进程permission-mode=acceptEdits已现场核实。所有失败和越权尝试保留，权限不放宽。前端新增客户端八路由参数/PRD跨项目历史反例实际2失败（日志/tmp/phase8-v7-project-frontend-red.log），尚未实施该补丁。

T批次完成（非超时），六文件专项131通过/7失败，其中6项是尚未实施的analysis/pipeline缺口，输入仍select+backup受误伤。新增隔离反例证实：SourcePolicy span里的upload/files仍被空anchor转回业务义务；中文关闭camera uploads后未共享条件span导致误添upload请求；mixed父比较全局两集合齐全不能证明逐产品模式；源文uploads屈折未归一导致真实能力假阴性。U以共享条件语法、政策范围剔除、通用屈折和原问owner×mode矩阵修复，不修改预算/严格数量/引文/否定/流程/版本守卫。比较测试先把源句改成base upload隔离该缺陷，另新增uploads独立正例，分别红；避免一个先发假阴性掩盖比较误阳性。

U超时退出但部分修改已落盘，实际进程结束后专项119通过/1失败。比较矩阵及uploads屈折正反例已通过；中文剩余错误已追到“移动端”里的“移动”被识别为动作，不是关闭条件未复制。下一批用平台实体span词法优先保护，避免靠列更多连接词。

新独立审查与红例还证实两项输入关系缺口：同问upload/files+delete/photos两项都满足后，额外upload/photos真资料可混入交付；合法comparison的action/object=null则固定失败。V按原问操作义务关联拒绝未请求的交叉leaf（原始分母不删、合法关系保留、未知绑定ID仍严格拒），support再核对关系资格；一般比较只使用匹配父原问义务的真实侧，空轴不等于任意来源通配。新增v-relation-red三项失败，含中文词法冲突。

Worker保护审计补充：P—U六批成功Edit/Write路径全部在各自下发白名单，未见key/.env/.git敏感读取。只解析工具名、file_path与tool_result固定错误分类，不读取thinking。P1次、R3次、S1次、U2次Bash尝试共7次均为No such tool，工具不可用、没有执行；T与Q无Bash尝试。脱敏元数据见work/phase8-supervision/20261017-implement-v7/worker-scope-metadata-audit.json。环境配置差异已核对：非登录环境未载入Tavily，原登录环境check-config正常；只作配置存在性检查，不算真实搜索/模型成功。

W批次前独立新增五个反例均红（w-red.log）：Android被mobile弱化、Shared tab被Other tab替换、正确manual-upload/files与automatic-delete/photos误添自动上传、one-file与multiple-photos数量跨关系广播，以及scope明确Android未生效。修正方向统一为InputObligation关系中的mode/platform/entry/quantity及原文span；候选不能定义自己的范围，具体约束按匹配关系生效，不能继续字段豁免。一般comparison空轴V2完成并由Codex独立四文件122项通过；这是专项成绩，不关闭真实质量Gate。W仍使用原Research线程，仅允许input/spec两文件，禁止工具权限扩张，测试由Codex负责。

W完成、子进程退出后独立127专项通过。代码复核发现仍按动作段提数量、按整anchor平台集合/第一入口判断，未真正实现关系字段；新增w2-red三例均失败：双平台交换误阳性、同动作不同对象数量误广播、两个正确入口误拒绝。W2继续仅input/spec两文件，以原文atom/span及动作—对象—owner关系约束替代旧全段先验检查；禁止用multiple wins或首项选择掩盖冲突。

分析/恢复下一批边界补充（源码未实现，不算成绩）：comparison是输出层，不能以父节点空轴充当同维度原子前提；分析必须匹配其显式产品/操作范围，但事实q1与分析q2可以合法关联，不能强制anchor相同。候选池区分已验证、暂缺前提、永久拒绝，每轮用当前支持账本重验；后轮冲突撤销依赖分析，其它合法分析保留。空premise evidence_ids代表服务器完整支持组，展开后检查实际ID并集≤40，非空错误子集仍拒绝。分析正文避免复制完整长流程导致3000字重复预算自阻断；保留结构化实际前提与事实链接、产品/步骤数/差额的有界推导，不截引用原文。

来源政策归位的证据来自旧真实原/扩展集：纯condition候选用完整Cite only官方span（有时action被写cite/reference或select）造成规划失败；混合候选同时含政策与“存储空间不足”非输入条件，不能作为纯政策删除。管线归位必须先闭式schema及唯一ID验证，仅完整policy span且全部condition_quotes均纯政策才可移出业务候选；原问义务分母仍由输入账本保留。重复ID/真正未知绑定仍拒绝；已知被拒候选绑定局部处理并记trace，不能修改冻结后指纹。

W2完成且子进程退出，独立129通过/1失败（w2-after）。剩余入口失败已精确追到不区分大小写的贪婪命名入口正则，把Shared tab到Other tab吞成一个实体；不是支持原文不够。新w3-red另确认scope手动模式/多选数量未生效、不同关系的null平台/app入口未逐关系回填。W3继续原线程仅input/spec，词法在首个完整UI marker停止、scope按原文atom补充到匹配关系，原问冲突保留局部缺口。

新的只读流程审查（尚未实测）：同一source同一heading下两套从1重编号的流程没有独立flow身份，底层_procedure可把A1+B2看成{1,2}；若offered含四步，闭包会补全部并因重复编号拒绝，因此完整offered时不能宣称混搭实际通过，但正确A1+A2也被扩成四步误拒。后续须先实测A完整/B完整、混搭/缺步、两步对三步、长步相邻片段；新增v7 entries.flow_id/flow_step_total并按真实列表身份约束支持与闭包，旧research_contract不改，不靠过滤污染换complete。

W3超时退出，子进程退出后独立133通过（w3-after），不能将job完成状态冒称completed。进一步scope与宽候选反例w4-red为6失败/1通过：原问scope冲突无gap、非空候选绕过mode_scope、宽null一次消费不同设备关系、scope入口未解析。W4只修上述契约匹配与冲突记账，不能先算fulfilled再把多值留空；原问和scope精确span都保留。此批仍未关闭输入Gate，更未关闭真实研究质量。

W4超时退出，子进程退出后139通过/1失败（w4-after），scope冲突/非空模式/入口已通过新增反例；宽候选仍失败，直接追到scope_conflict清空匹配后未重算fulfilled_ids，旧union仍算已满足。新正确拆分正例2失败（w5-positive）：同action/object不同平台/入口的兄弟关系在_fulfills直接抛错，误伤合法拆分。W5缩至spec单文件内部匹配与结算：逐关系先不匹配，候选整体无合法关系才保留明确拒绝；最终回填和歧义处理后重新结算原义务分母，不扩大范围。

W5完成且子进程退出，独立四文件142项通过（w5-after），包括正确拆分与错误合并对照。新分析8失败/6通过（x-answer-red），采用两套真实结构前提而非伪造产品字段；32片段长流程虽≤40，复制引文使描述超3000，已单独复现。新流程6项中5失败/1通过（x-flow-answer-red的流程部分），确认A1+B2混搭可误阳性、正确A闭包被误扩、两步A被三步B抬高步骤数。管线x-pipeline-red为5失败/12通过，涵盖纯政策候选归位、独立原问义务/已提供flow目录、已知拒绝绑定、暂缺前提恢复；合法旧分析保留正例已过。材料化x-artifact-red为16失败/2通过（旧v1/v3兼容正例过），新增收益承诺先拒、真实用户愿望引文保留、调用级预算脱敏及真实来源索引并集。上述均为实施前红例，不修改旧测试/迁移/Prompt。X-answer只允许answer单文件，之后再分批流程/管线/材料化/项目隔离。

W5稳定只读复查另指出数量同轴剩余路径（未运行新例）：同anchor重复upload one photo与upload multiple photos，早期anchor-wide回填令每r.quantity=multiple，_fulfills不比数量，后置歧义guard只在quantity空时检查，因而一候选能消费single/multiple两关系，正确拆分也被抹成multiple。平台/入口的最终union重算已静态确认，不能把该数量缺陷混称旧stale-union未修。分析Writer结束后须先补数量同轴宽候选负例及正确拆分正例，再按原proposed_quantity与原问关系匹配/结算；原问无数量时不得接受模型虚加多选。

X-answer完成，源码指纹与261项manifest相比仅answer允许文件变化。首轮12通过/2失败是本轮新正例fixture的analysis.products仍只继承OneDrive，却要求引用Dropbox；按原问两产品显式修正fixture范围后14通过，未弱化断言/守卫。继续源码复核发现候选被原地展开污染审计、request动作/对象未匹配、两不同产品同一步数仍生优劣、counts与排序后的产品未对应且优缺点正文相同。新增x2-answer-red为5失败/13通过；X2仍仅answer单文件，必须实际显示逐产品计数/差额及不同的待验证假设。数量/设备轴新实测x-axes-red为5失败/1通过，确认同对象single/multiple早回填抹分母，以及同一动作显式Android+iPhone被单Android消费；分析Writer结束后实施对应关系拆分/匹配，不能忽略这些原问义务。

X2-answer完成、进程退出，最终该专项18通过（x2-answer-after-fixture）。首复跑16通过/2失败来自Codex新equal-step工厂只替换了要求参数而未替换Dropbox来源正文，实际在支持前置断言失败，不能将这两次算成原实现语义反例红证据；修正来源参数后用两个真实完整同操作产品流程复验通过。其它候选不变、同产品错误操作及产品—计数对应/优劣差异反例均已独立闭合。X-axes续原Research线程，只允许input/spec，处理x-axes-red已实测的同对象single/multiple及同动作共享两个具体设备；原问无数量不让模型增多选，明确多个值需拆分并保留原义务分母。

X-axes完成、进程退出后五文件164通过/2回归失败：原问普通复数、模型虚加multiple时误拒整关系；select multiple photos在backup动词前的数量被当unspecified而误伤真实数量正例。新增同数量/设备分母反例已经通过，不能据此忽略旧正例。X-axes2仍仅input/spec，原问无数量时归位为unspecified并保留正常事实，具体数量跟随前/后置对象短语；数量scope原文text/start/end必须相符，不能canonical字符串配对象span。流程新红例改用实际v7 catalog重建（旧helper默认旧catalog，断言未改），y-flow-red仍为5失败/1通过。新鲜Git现场仍HEAD449e879、原分支且index空；89项旧测试/迁移/Prompt/ZIP/密钥/AGENTS保护指纹无漂移。

X-axes2完成、进程退出，五核心专项166通过（x-axes2-after），新增数量作用域原文slice正例另1通过（x-quantity-provenance）。Y-flow开始，仅catalog_v7/support_v7两文件，加入快照/section起点/列表起点派生flow_id和flow_step_total；只先修底层身份/完整性，不修改旧research_contract。下一批pipeline再按flow接闭包，因此Y批单独不能以两个闭包正例仍红宣称底层失败或整体完成。普通导航步骤、长行全部片段、同Run同项目、原预算和严格数量/条件/版本/否定守卫全部保留。

Y-flow完成，六文件169通过/4失败（y-flow-after）：混拼与两步/三步分离已正确，两个闭包失败按分工待下一批；真实Dropbox条件+八步、OneDrive已有图片分支回归失败，不能记底层PASS。根因是任一列0普通段落就断流，且非编号条件/分支flow_id空，与步骤多组冲突。新增y2-flow-red两项均失败：没有Method标题而编号重启、连续步骤中间插Note。Y2仅catalog/support，按编号重启/section隔离、连续编号跨普通说明保持流程；唯一明确流程的前置条件/分支/尾注继承，多个流的歧义说明不可随意借用，不删除说明或降级到宽松fallback。闭包仍下一批按flow_id接，预算不变。

Y2完成且子进程退出后独立复验25失败/150过（y2-flow-after）：catalog返回3项、调用解包4项导致运行时ValueError，Worker报告中的预期通过不作为证据。新只读复核另确认section上界缺失和多流程过渡说明任意归前流；新增两个边界回归，Y3只允许catalog/support修复接口一致、section范围及首末编号跨度归属。Codex继续执行原45号方案及独立验收，未暂存/提交/push。

Y3完成、子进程退出，合并catalog/六核心/pipeline实际192通过6失败（y3-flow-pipeline-before）：新增流程上下文边界及冻结样本已过；6失败均为下一批pipeline已知范围。只读预算复核更正：SupportLedger.update已经原子检查并集40且在超限时拒绝更新，不能声称当前一定在最终budget.select崩溃；新增验收应聚焦空premise展开后的selected与账本联合预算及冲突撤销。

Z批前新增独立冲突撤销与38/39联合预算对照，实际2通过1失败（z-budget-conflict-red）：冲突能撤销依赖分析且保留无关，38诊断+2账本的40边界通过；39+2=41仍误报completed，已实证。另来源政策重复ID/混合条件/不完整policy三负例均通过。Z仅pipeline及三个新v7 Prompt，实现flow闭包、raw候选24/交付8重验、展开后的联合40预算、原问义务/已提供flow目录及纯政策归位。既有严格guard与预算不改。

材料化实施前只读复核补充：只对新AI v2/v4执行原candidate parse/商业承诺→Raw映射冲突检查→known逐字风险→实际引用事实材料化→最终parse。Raw收益愿望的豁免必须有服务器字段完整渲染与ID/span逐字proof，仅扫描副本遮蔽该Raw，不能跳过整章或凭用户报告前缀放行。各章引用集合先冻结，全局索引另取实际章节/风险并集；完整包装后校验4000/500及schema列表预算，超限不截Raw、协议异常不带正文。该方案尚未实现，不作验收成绩。Y至Y3元数据审计成功写入均在下发白名单，三批各一次Bash尝试均工具不可用未执行，89项保护指纹再次无漂移。

Z大批sync_timeout退出，固定工具元数据未见Edit/Write，源码无落盘改动，不算实现。拆为Z1候选/预算单文件和后续Z2结构/政策/Prompt。新增同facet八条合法机会堵后轮风险红例失败（z-facet-red），最终8槽须先覆盖各请求facet的合法代表，再填充重复facet，避免只把永久坏候选换成重复合法候选后仍自阻断。Z1仅pipeline单文件，沿用原线程。

材料化/项目隔离实施前新鲜复跑，默认临时SQLite夹具实际24失败7通过（aa-artifact-project-before-default）：材料化16红、旧兼容2绿；项目跨范围8红、正确/省略上下文及不存在资源5绿。首次手动TEST_DATABASE_URL=sqlite://造成HTTP夹具跨线程配置异常（aa-artifact-project-before，29红2绿）已撤回该环境覆盖后重跑；那5项额外失败不作产品缺陷证据，未修改旧夹具。

Z1完成、进程退出后pipeline专项21通过3项Z2待修；源码复核新增两个预算反例均红（z1b-budget-red）：把全部历史fact_pool误当交付账本导致合法小集合被拒；只审新候选未审历史候选，第三轮联合41仍保留旧分析。Z1b仅pipeline，统一raw archive后逐条语义解析与采用时预算，以当前ledger交付ID为base并保留暂缺raw，不用历史诊断池或旧analysis_rows污染预算。独立临时PG的Raw当前12行9字段再次与原快照逐字段相等，SHA不变；这仍是中间审计，最终同源码链路后须再做。

Z1b完成、进程退出后pipeline+flow实际31通过5项Z2已知红（z1b-after），原分析和联合预算正反例已闭合。Z2续原线程仅pipeline+三个新v7 Prompt，实现flow闭包、纯政策归位、已知拒绝绑定及原问/flow输入。Codex在Z1b退出后新增跨字段收益愿望借豁免测试：Requirement两项红、PRD三项过（aa-wish-borrow-red）；前端加入不依赖abort的两个延迟详情/历史响应反例，新三项红（/tmp/phase8-v7-project-delayed-red.log）。未更改保护测试。

前端实施范围补充：PRD重新生成目前发送空对象，使用旧v3默认。对已由新v4生成的PRD，重新生成必须明确继续v4；历史旧版本保留旧请求契约，不改默认配置或保护测试。新增测试须验证新v4路径实际prompt_version，最终真实链只统计明确v2/v4产物。

Z2完成、进程退出，八文件207项通过（z2-after）。审查继续新增4项实际红例（z2b-policy-red）：policy候选删掉前未校验原schema，缺字段/额外字段被洗成合法；合法指向policy的query未随候选移除；scope中的完整policy未归位。Z2b仅pipeline helper，先验证整个原候选/原ID唯一/原query引用合法，之后按同一政策grammar精确处理question及2000字符scope，过滤对应query且不动冻结指纹。前端v4再生成专项已加入新测试（保护旧mock契约不改），测试尚红；不是已实现。

Z2b完成、子进程退出后八核心实际211通过（z2b-after）。转AA1只新建artifact_grounding_v2核心，AA2再接服务/新Prompt。实施前补未知userID精确口径、FR/edge/AC/指标专用引用索引及实际全局33>32预算拒绝测试；公共helper不执行known风险gate，调用层另做原candidate先验与Raw冲突/known顺序。proof仅服务器生成并记录精确Raw spans，最终扫描不豁免模型文本。

Research旧兼容专项独立493通过（z-research-compat），非最终全量。并行只读审查提出新的具体待复现边界：旧支持40条+第三轮反证1条使ledger拟合并冲突41而预算原子回滚，pipeline跳过分析重建，可能仍交付旧机会；不能称已实证。AA1退出后须增加39/40+1冲突对照再修，当前不另启写入者。候选语义拒绝diagnostic索引/采用来源也列入只读审查。

并行只读Research审查收敛为四项待运行反例（已停止扩范围）：①40旧支持+1反证时预算原子回滚可能保留旧分析，同时旧comparison.side_requirement_ids依赖失效后未撤销；②代表遍历可能采用>8分析（填余槽有8限，代表遍历无）；③后置设备短语on iPhone/on Android可能被交换到另一动作；④语义拒绝都来自analyses([raw])的index0，预算拒绝又用筛后索引，缺统一attempt/slot/candidateID采用来源。须AA1退出后补实际失败证据，分批修；目前不能称已复现。

AA1 sync_timeout退出，未创建模块、无Edit/Write，不作实现。先收敛只读审查的运行证据：ab-independent-review-red为4失败4通过，实际确认40+1冲突回滚保留旧前提、10行分析突破8、后置设备对调、比较父项未失效；39+1、8行、前置设备、同极性比较四对照绿。新增ab-provenance-red实际缺proposed_attempt。AB仅support/pipeline修冲突/比较失效、8硬门和服务器candidate_id/attempt/slot，后置设备下一批。AA材料化尚未落盘、仍待实施，不混作已完成。

AB job虽completed但空响应、1 turn、零工具/写入，不能算实现；ab-after实际仍4失败3通过（仅pipeline/support选择，未含后置设备）。沿原线程拆AB1 ledger两个失效修复和后续AB2 pipeline硬上限/来源元数据。AA1尚未落盘，后续须拆更小核心批继续；保留全部失败证据，不提交。

AB和AB1两次续接均success/end_turn但空响应、各6 output_tokens、零工具/写入，原threadId确认未变；仅检查record type/usage/stop/session固定元数据，未读取thinking，原因未证实。沿原线程以本次明确systemPrompt+第一步Read+support单文件再次续接，edit/simple和工具白名单不变，不修改配置/密钥/权限，不新开线程；仍不把completed当实现。

2026-10-18用户新指令：不再让Claude Code修改，改由Codex亲手实现。最后Worker已退出、无写入，此后源码/测试/文档由Codex单写入。已亲手修复AB冲突/比较失效、8行硬上限、candidate_id/proposed_attempt/proposed_slot，以及后置设备关系；八核心220通过。新增artifact_grounding_v2及v2/v4原candidate先验→Raw冲突/known→完整引用事实材料化→精确Raw span proof最终扫描；八路由project_id前置守卫，正式前端可选参数全接线、scope ref丢弃过期详情/历史、v4再生成继续v4及中文缺口fallback。材料化/API42通过，前端182通过/lint/build通过，Ruff通过。未知用户计数新fixture原工厂仅引用F001，已显式改为四个实际引用后验证，未扩大产品引用集合。
SQLite1464通过11跳过；首PG空库因未先迁移缺vector类型失败（420 setup errors）属验收脚本初始化缺失，未改产品迁移或用户库。修正全新临时PG先upgrade head后，1475通过，另一全新临时PG upgrade/check/downgrade base/upgrade/check通过。Python运行环境使用现有API解释器cache/uv/...，.venv本身缺pgserver，不安装/修改依赖。
第一轮本次真实original/extended固定分母完整执行，目录20261018T082746Z-research-agent-v7-{original,extended}，仍多项PARTIAL，未作质量PASS。新实际定位mobile宽scope误判Android、比较后省略宾语的自动前提被删、普通数量/动作短语误作条件、独立select/start导航候选阻断整个规划。已修scope域交集、同动作显式模式省略宾语、HOW作用域及非业务条件归位、闭式原schema后辅助候选局部归位；八核心223通过（codex-live-contract-after）。三新增fixture先后重复参数/默认files错误均先修正，最终真实三红后再修产品，错误夹具失败不作产品证据。此后需同最终代码重跑完整工程与真实题集、原文审核、链路及保护审计，仍PARTIAL不暂存/提交/push。


## 2026-10-18 文档交接检查点

三文鱼要求总结当前进展、痛点、处理方案并转新对话。新增47号主交接和48号续接Prompt，更新入口；Codex亲手实施的最新授权继续有效，不再续派Claude。

最后亲手修复限定否定范围和产品平台owners交叉配对，新增正反例；八核心最新230通过（codex-handoff-core-230.log）。最新四文件Ruff check通过，但support_v7及其新增测试2文件format仍红，交接不再改生产代码，明确留待下一轮。SQLite1464/11skip、隔离PG1475及迁移往返、前端182/lint/build均早于最新Research修复，不当最终成绩。

第一轮真实原10为2completed/8partial、另8全partial；第二轮原10/另8均全partial，各有9次provider_failure事件。日志没有HTTP分类，独立shell缺Tavily配置也不能解释此前真实Run原因。MiniMax搜索工具找到微软官方页仅证明Codex工具可用，后端独立替代provider尚未实现/验证。Dropbox已读全文未完整提供、另一题混绑流程、中文/分析协议失败仍待修；不得放宽守卫。

交接只读确认当前PDF Research 53763da7-b422-47a3-adb5-0458aa4bc72b为partial_failed/object_unknown，新链PM/Requirement/PRD尚未开始；旧照片链失败留存。Raw12行9字段再次逐字段不变，SHA bf8ff4e186f47965f7ff1c99d845c145ab761f7cd4ce2ec72f3063deaf03fecd。自有临时PG revision0015，确认data_directory为本轮e2e后只读导出备份；服务保留续接，用户库未动。

HEAD449e879、index空；本次明确选择80项保护项0漂移，不能冒充此前89项全清单。现场摘要、源码指纹、脚本/前端日志副本及owned-e2e-pg.dump存work/phase8-supervision/20261018-handoff-v7，不进Git。Phase8仍PARTIAL、质量门槛未过，正式验收NOT MEASURED；未暂存/提交/push。本轮按用户要求文档交接后结束，下一对话继续Phase8，不进入Phase9。

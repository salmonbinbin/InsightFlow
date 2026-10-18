> **最新交接（2026-10-17，深度根因分析）：Phase 8 PARTIAL，Research质量FAIL，未验收、未提交、未推送。** 现有完成门槛不验证整题覆盖，检索/引文表示/软降级纠错另有断点；本轮只做分析与交接，没有新产品修复或真实模型验收。请先读[36号根因与新对话交接报告](36-Phase8Research根因与新对话交接报告.md)及[37号启动Prompt](37-Phase8新对话启动Prompt.md)。36号报告已更正旧Dropbox overnight归因、iCloud缺risks分类和跨行引文的笼统解释；以下为历史记录。Phase8后STOP，不进入Phase9。

# Phase 8 Research 分层定位与本轮复验（2026-10-17）

负责人：三文鱼。执行：Codex Supervisor → 原 Claude Research Worker。

**最终结论：PARTIAL，真实 Research 质量 FAIL；未验收、未提交、未推送，不进入 Phase 9。** 本轮已在最终 v4 代码上完成全量回归、十条真实 Tavily + MiniMax 调用、官方全文核对及真实浏览器联调。十条为 5 completed / 5 partial_failed，Codex 判定 2 完整回答 / 3 局部事实 / 5 未验证；3 条 completed 不能当作整题完成。负责人正式人工签字及长期语义指标仍为 NOT MEASURED。

## 1. 核对与保护

- 开发前实际 HEAD 为 `449e879`，存在大量 Phase 8 未提交代码、文档、`.playwright-cli/` 及两份用户原型 ZIP。未清理、重置、自动暂存或覆盖这些文件。开发前文件 SHA-256 清单位于本机忽略目录 `work/phase8-supervision/20261017/initial-manifest.json`。
- 当前工作在 Phase 8；代码迁移头为 0013。用户运行中的两套 PostgreSQL 数据目录为 `work/local-postgres` 和 `work/phase7-user-postgres`，只读连接均返回 `could not open file global/pg_filenode.map: Operation not permitted`。**用户库实际 revision、表与行数 NOT MEASURED**；未降级、重启或修复用户库。
- 8000 端口所属工作目录是另一项目 `AI小商/backend`，未干预。Phase 8 验收使用自己新建的临时 PostgreSQL 及 18080/13080 端口。
- `key/.env` 只有原有 MiniMax/Anthropic 配置。最初从后端目录启动的进程未继承 `TAVILY_API_KEY`，项目没有 Tavily 配置文件；后续确认根目录启动环境可以读取该变量。只检查配置字段与可用性，不显示、修改或提交密钥。初始后端目录启动的诊断在外部请求前因缺 Tavily 终止，不计入真实样本；最终从根目录启动独立进程，成功完成十条真实调用。没有声称三文鱼新增或修改过配置。
- 本轮开发前独立基线：Research 267 passed；SQLite 956 passed / 11 skipped；新建隔离 PostgreSQL 967 passed，完整迁移 `upgrade/check → downgrade base → upgrade/check` 与 `POSTGRES_VERIFICATION_PASSED`；Ruff 通过。

## 2. 判题口径：来源支持与回答完整性分开

每题先列明产品、平台/入口、操作、数量、限制/否定及时间/版本条件，再将每个子问题对应到官方原文。引文存在、来源 ID/URL 合法、API `completed`、人工完整回答是四个不同层级。

- **完整回答**：题目要求的全部关键条件都有相应原文；未知限制明确说“未验证”，不写成“不存在”。多步问题不能只引其中一步。
- **局部事实**：引文和产品/平台语境能支持一个窄事实，但缺少所问操作、数量或其他子问题。
- **未验证**：没有合格目标事实、检索片段不足、结构失败，或无法恢复原始输出链。保持 `partial_failed`/`failed`；不能由缺页推断产品不支持。
- **误阳性分开记录**：事实引文不受支持，与将局部事实包装为完整答案/市场模式，是不同错误；不能把“逐字引文真实”当作两者都没有问题。

程序当前 `_has_external_confirmation` 仍只要求至少一条带来源的 competitor/market_pattern。它不逐项证明完整性。为保持现有 API/状态与严格校验，本轮采用明确的页面完整性提示，**没有实现通用自动语义完整性裁决器，也没有让模型自评充当验收**。

## 3. v14 十条逐层复核（历史回放，不是本轮真实调用）

原始 v14a/v14b JSON 在本机仍存在。已保存外部来源、发现与诊断到 [脱敏外部证据](evaluations/phase8-v14-layered-evidence.json)。不包含 Raw Feedback 或密钥。

历史数据没有逐次模型原始 payload、每次结构错误、来源过滤前全量返回；降级发现还清空了原始 source_refs。因此以下“候选来源检查”是按逐字引文重建的诊断，不能冒充已恢复原始引用或整条执行链。对空数组不能武断归为 Schema 错误或校验误杀。

| 轮次 / 产品 | 实际 Run | 分层定位 | 回答判断 |
| --- | --- | --- | --- |
| v14a Google Photos | `ca7f0301-fb62-4091-b06d-204aaa7038e2` / partial_failed | 保留 5 个官方来源，但混入备份质量、其他 App 图片及 Desktop 页面；保存片段没有直接说明 Android 手动多图一次备份。无 competitor/market_pattern 留存；精确模型决策不可恢复。 | 未验证；官方域命中不代表产品/平台/操作相关。不能推出“不支持多选”。 |
| v14a Dropbox | `8f37be12-6f8f-46de-8f86-3dfa9359bcfc` / partial_failed | 已有官方 `upload-mobile`，片段写手动添加与 camera uploads 区分，但没有目标多选数量/操作步骤；其余有自动上传及格式页。没有确认目标发现。 | 未验证多图一次上传及限制；不能把存在手动上传等同明确多选。 |
| v14a OneDrive | `0bb2b5db-bda3-47c1-b1c8-b7eb3b90de3a` / completed | “Choose Existing…Done”引文本身真实但无显式数量，按多选守卫降级正确；通过的是 Troubleshoot 页双指选择 workaround。同一句还被归成单来源 market_pattern。 | **局部事实**。双指选择 OneDrive 中项目的 workaround 不足以证明 iPhone 本地照片 picker 一次上传；单产品步骤不是市场模式。 |
| v14a iCloud Photos | `b0d31a38-c67b-45bd-86dc-d9031ab82db8` / partial_failed | 官方片段涉及自动同步及共享图库移动，与 iCloud.com 手动多图上传不等价。重建候选检查中两个标题的 name-support 均失败，引文还都缺显式数量。 | 未验证；不能把同步、共享图库移动与网页上传混同。 |
| v14a Google Drive | `2927e516-1722-49f7-8b05-2a2089c5a6f4` / partial_failed | 官方来源多为同一帮助页不同查询参数，另有入门页；一般上传句仅写 `Find and tap the files you want to upload.`，没有明确多选数量。无确认发现。 | 未验证一次多选；同页不同 URL 不能算独立市场证据。 |
| v14b Google Photos | `5e3458d2-2b78-46c9-879a-360385c266c5` / partial_failed | 题目已缩为单张手动备份，官方片段有 `Open the photo or video.`。两个候选 market_pattern 标题加入 manual/steps/single 等操作性概括，名称匹配失败；产品步骤错误归成市场模式。 | 有可用窄原文，但该 Run 未形成合格目标发现；这是名称/类别及输出策略损失，不能归为无官方资料。 |
| v14b Dropbox | `3fa7ca34-de32-497a-8001-3815980b96d2` / partial_failed | 原问题 `manually add photos when camera uploads are disabled` 被查询压成 `Dropbox photos site:help.dropbox.com/create-upload`；关键 `manual/add/mobile` 丢失。保留 4 个无关度较高页面，没有 `upload-mobile`。 | 检索语境损失；没有确认答案，不能推断官方没有手动上传资料。 |
| v14b OneDrive | `8e15eb36-1fa3-4d5f-9390-5da23f1f2995` / completed | “If you tap Choose Existing…select them…tap Done.”在官方 iOS 手动上传页逐字存在。另一个 workaround 标题含来源不支持的操作词，被 name-support 降级。 | 对这道已缩窄的“如何选择已有图片上传”问题，确认引文回答相应步骤。**不能迁移为 v14a 的多选、入口和自动上传区分均已验证。** |
| v14b iCloud Photos | `f8d51c18-a421-4389-9545-72424c3b7b4d` / partial_failed | 引用要求先写 Apple Support，旧查询构造取最早品牌，实际 query 为 `Apple Support upload site:support.apple.com/en-us`；iCloud.com 和 Shared Photo Library 语境丢失，结果包括“Unable to Upload”等其他流程。无确认发现。 | 产品/入口检索失败；精确模型输出失败原因无原始轨迹可恢复。 |
| v14b Google Drive | `83a3291e-727e-402f-9dcb-38c0a4e94587` / partial_failed | 模型把 3 行编号步骤拼进一个 Evidence 引文。原片段并无该连续字符串，严格引文校验降级；混入 iOS 结果也说明官方过滤没有证明平台适配。 | 输出格式失败，不能放宽逐字/单行规则；应多条单行事实逐步表达。 |

本次按更严格“整题完整性”重新解读历史完成项：**1 条回答缩窄问题、1 条仅局部事实、8 条未验证**。这是对已有十条的 Codex 来源判断，**不是最终 v4 代码的真实成功率**。v14a OneDrive 的局部发现若被展示为整题完成，属于完整性误阳性；单来源 market_pattern 也有类别语义错误。两个 completed 的描述有原文，不等于标题、类别、入口和所有子问题都已证明。

### 本轮官方页面核对

本轮重新读取 Microsoft 两个官方页面：

- [iOS 手动上传](https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios)原文：`If you tap Choose Existing, links appear to the Photos app on your device. Browse to the pictures you want to upload, select them, then on the top bar, tap Done.`
- [iOS 问题排查](https://support.microsoft.com/en-us/onedrive/troubleshoot-onedrive-for-ios-app-problems)先写 `Users of iOS 16 may not be able to use the two-fingered pan gesture to select multiple files in OneDrive.`，然后 `Workaround` 下写 `Select one photo first, then use two fingers to slide to select multiple files or photos.`。必须保留 iOS16/workaround 语境，不能概括为所有版本、所有入口均支持。
- 本轮可读的 [Apple 101984](https://support.apple.com/en-us/101984)是 `If you see "Unable to Upload" in the Photos app`，包含重新导出/导入失败照片的特殊流程，不能用于一般 iCloud.com 上传选项题。
- 本轮最初 Google、Dropbox 直接页面抓取超时；随后通过联网读取工具成功取得 Google、Dropbox、Apple 与 Microsoft 官方全文，最终十条判题使用本轮原文。历史回放和最终真实结果分开记录。

## 4. 最小修复与 Supervisor/Worker 记录

Research 原 `threadId=8d048dd4-fb66-4838-898b-44f10cedd34c`：

1. job `46a27c79f3914bb59c69e8efe069c1e2`：写入官方查询修复和独立 v4 Prompt，保留 v1—v3。Worker 测试命令被权限拦截，**没有可采纳红绿结果**；还执行了未下发的只读命令，已在续修中改为禁止所有 Bash，测试由 Codex 接管。未改权限配置。
2. Codex 首轮专项：**1 failed / 278 passed**，新版 Run 为 v4 但测试 stub 仍报 v3。Ruff 三项失败。Codex 另写行为测试，实测 **2 failed / 14 passed**：任意 `\\w*` 词形匹配误将 viewpoint/shareholder/editorial/storagecorp 识别成 view/share/edit/storage；iCloud 修后仍丢 `.com` 和 Shared Photo Library。多条逐字单行步骤通过、跨行合并仍拒绝的两项行为测试通过。
3. job `19900ffa7c8b4dcd83d4554eeadd2c8d`：带上述精确失败、原问题和 Microsoft 原文语境续接同一 Research 会话；限定词形、保留明确产品入口、修 stub 和 Ruff、补平台/流程/时间及局部答案规则。没有授权放宽来源或完成门槛。

4. 原前端 `threadId=42e973e0-5295-496d-a9bd-7c08d6d24ae4`，job `dc6b1408ebd745a0863488060f07d2af`：仅修改 Insight 详情及测试，新增所有终态的“回答完整性：未验证”提示；不由 confidence、confirmed 数量或模型自评判断完整性。新增三项行为测试，覆盖全 confirmed 的 completed、partial_failed 与项目切换后的旧响应清理。
5. Codex 接管后机械整理新文件导入/格式，并纠正新版 Prompt 两处歧义：单来源市场模式为输出禁令，不能称服务端已验证市场语义；无来源的 status 规则只适用于 opportunities/risks。格式整理使文件超过建议行数 1 行，静态测试失败后仅压缩模块说明，保留断言并重跑全量。未改核心守卫。
6. Worker 自述“没有运行命令”不作为事实：第二 Research job 日志实际仍有只读查询/计数命令。没有观察到 Git、密钥、权限或保护文件写入；Codex 的清单比对和最终独立测试才是验收依据。

全部 jobId、日志路径及轮次证据另存本机 `work/phase8-supervision/20261017/`。同一工作树各轮始终只有一个代码写入者；Codex 在 Worker 完成后再写独立测试和文档。

## 5. 最终代码独立验收

| 检查 | 最终实际结果 | 边界 |
| --- | --- | --- |
| Research 专项 | **283 passed** | 原 267 + 新增 16；其中部分为静态 Prompt 契约检查，不是 283 次模型评测。 |
| SQLite 全量 | **972 passed / 11 skipped** | 11 项为 PostgreSQL 等专用路径；不能代替 PostgreSQL。 |
| 新建隔离 PostgreSQL 全量 | **983 passed** | 真实 PostgreSQL 16.2；包括 Tool 并发事务回归。 |
| 完整迁移往返 | **PASS** | fresh `upgrade head → check → downgrade base → upgrade head → check`；`POSTGRES_VERIFICATION_PASSED`。没有对用户库降级。 |
| Ruff | **PASS** | `app tests alembic scripts`。 |
| 前端 lint / test / build | **PASS / 147 passed（15 files）/ PASS** | Next.js 14.2.35，11 个业务路由及 not-found；构建静态页面 11/11。 |
| 真实 Research | **已测，质量 FAIL** | 10 条真实 Tavily + MiniMax-M3 / research-agent-v4；见下节。 |
| 真实 FastAPI + Next.js 浏览器 | **工程流程 PASS** | 回放展示、缺配置错误、真实 Research 调用、Sources 和项目隔离、模拟审批；不代替语义质量。 |

最终日志在 `work/phase8-supervision/20261017/phase8-final-*.log`。代码 AST 对比仅 `_scan_semantic_terms` 和 `_build_official_provider_query` 函数变化；官方规则、校验函数、完成门槛和事务路径与本轮开始相同。原型 ZIP 哈希保持，旧迁移、Raw 模型和 Phase1—7 业务文件未新增改动。

### 5.1 最终十条真实调用与原文判断

完整脱敏输入/来源/模型原始结构/校验结果与 Codex 判题在 [v4 真实证据](evaluations/phase8-v4-live-evidence.json)。内部输入为明确合成种子，不是用户业务反馈；所有 Run 属于已关闭的临时库，不能再用 UUID 在线查询。

| 序号 / 产品与题型 | Run / 程序状态 | Codex 原文核对与原因 |
| --- | --- | --- |
| 1 Google Photos 多图手动备份 | `a8ae9457-180b-4c2a-8c19-cdfb263efdbf` / partial_failed | **未验证**。官方 Android 帮助全文的 Tips 明确有多图备份；Tavily 片段却截到 Tips 后跳转别处，漏掉关键句；query 又未保留 several 的数量信号。首轮 source_refs_invalid，重试仍引用跨行编号步骤，被守卫降级。不是“官方没有资料”。 |
| 2 Dropbox iPhone 多图与限制 | `4d909d70-7542-4585-8b7e-4de5277afc3d` / partial_failed | **未验证**。模型引用手动添加、overnight 与格式页，没有满足所问多选数量/手动入口；一条 overnight 句还指向不含该句的 source。缺证据及错配被降级，不能改成否定能力。 |
| 3 OneDrive iPhone 多图手动上传 | `85216389-f1a1-489d-9d13-1260d8ac01d6` / completed | **局部事实**。确认引文是官方 iOS16 双指手势限制，不是本机 picker 多选上传完整流程；完整性误阳性。 |
| 4 iCloud 三种入口及多图 | `2579bee6-7d90-4cd2-9528-4e4bffb7d8e0` / partial_failed | **未验证**。先出现未知顶层字段，重试后共享图库移动、自动同步及网页选择仍不满足精确引文/数量要求；保持降级正确。 |
| 5 Google Drive Android 一次多选 | `d55a6d3c-662b-4123-911e-89cb5375d510` / partial_failed | **未验证**。检索命中普通上传页，模型拼接跨行步骤；一般复数 files 不能证明明确多选，守卫继续拒绝。 |
| 6 Google Photos 单张手动备份步骤 | `72fd1637-e6d6-4e51-bcdc-97662fcd6c0f` / completed | **局部事实**。官方全文支持其漫游开关、自动备份流量限制引文；未给出所问手动步骤。完整性误阳性。 |
| 7 Dropbox 禁用 camera uploads 后能否手动添加 | `1e0f9410-3df3-4ebd-8f14-f787d3ec2d52` / completed | **完整回答该窄问题**。官方 upload-mobile 页明确说明相机上传未启用时仍可手动添加照片；没有扩写多选数量。 |
| 8 OneDrive 如何选择已有图片手动上传 | `551a53d9-b13f-4fe4-b9cb-b2f1fd1e1142` / completed | **完整回答该窄问题**。官方 iOS 手动上传页支持 Choose Existing 后进入图片、选择并 Done；另一个 iOS16 限制仅作其版本范围内事实。 |
| 9 iCloud.com 上传选项与 Shared Library 位置 | `f34d4fc4-fc99-45c7-8b67-2bd285bbdf52` / completed | **局部事实**。确认的是查看图库、共享图库邀请/相机分享等片段，官方原文不能据此证明题目所问的 iCloud.com 上传选项及上传位置。完整性误阳性。 |
| 10 Google Drive Android 普通上传步骤 | `8f1e4a00-45bd-45dc-9c9e-a1f05d2063d0` / partial_failed | **未验证**。官方步骤来源存在；三次真实模型响应均缺 submit_research tool_use，未落不合规发现。 |

**程序完成率 5/10；按整题判定：完整回答 2/10、局部事实 3/10、未验证 5/10。** 3/5 completed 不构成整题完成。对完成项观察到的引文窄语义有官方支持，但不据此宣称整体幻觉率 0；标题、分类、所有 unverified 推断和负责人标签仍需审查。Research 总体质量 **FAIL**，不提交。

关键原文定位（只列短摘与内容位置；完整页面不是 Tavily snippet）：

- [Google Photos Android 官方帮助](https://support.google.com/photos/answer/6193313?hl=en&co=GENIE.Platform%3DAndroid)：Manually back up 下有单张操作，Tips 另明确多张可长按选取后备份。该关键段在本轮持久片段缺失；不能用 snippet 缺句推断不存在。其漫游设置属于 Change your backup settings，不能回答单张手动步骤。
- [Dropbox 官方移动上传](https://help.dropbox.com/create-upload/upload-mobile)：How to upload photos and videos 明确 camera uploads 未启用时仍能手动添加，后列照片选择、位置与上传步骤。普通复数表达不用于绕过明确数量守卫。
- [Microsoft iOS 手动上传](https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios)：Choose Existing 所在 Note 支持已有图片选择并 Done；[iOS 问题排查](https://support.microsoft.com/en-us/onedrive/troubleshoot-onedrive-for-ios-app-problems)的手势说明明确限定 iOS16。
- [Apple 查看图库](https://support.apple.com/en-us/118447)是访问/查看照片，不是网页上传选项；[共享图库](https://support.apple.com/en-us/118229)中的加入和相机共享也不能替代 iCloud.com 上传流程。
- [Google Drive Android 上传](https://support.google.com/drive/answer/2424368?hl=en&co=GENIE.Platform%3DAndroid)有四步普通上传流程；第10题失败在模型协议，不是检索不到。

### 5.2 最终浏览器和数据库审计

1. 新建隔离 PostgreSQL + 真实 FastAPI 18080 + 生产 Next.js 13080：明确标注“隔离回放夹具，非真实调用”的 completed 与 partial_failed 都显示“回答完整性：未验证”；展开引用和 Sources 可见。缺后端目录启动配置时，点击 Research 显示配置错误，HTTP409，未伪造新完成 Run。
2. 同一最终代码，在根目录启动的另一新建隔离库中由浏览器真实点击 OneDrive 问题，落库 Run `53061d7e-24c1-401d-98a4-8a5ecc93b953`：`completed`、tavily、MiniMax-M3、research-agent-v4、5 Sources、6 Findings、12384ms；页面同时显示完整性未验证和 5 条 Not Verified。该 Run 只作工程联调，不混入十条质量分母。
3. 真实 Run/Sources/Findings 带项目 B ID 都为404；页面切换 B 后显示“该洞察不属于当前项目”，没有继续展示 A 的研究或完整性提示。第一隔离库对 Research 历史也分别验证 A200/B404。
4. 批准 Requirement `52af4cc8-7e73-4604-a0be-8a1ec22d9502`：浏览器 Prepare 核对冻结 Title/Description/Priority/Source Insight → Cancel，API 为 cancelled、external_id NULL → Prepare → Approve，显示 `SIM-27e1f9ba97184b1b9c1e5579`、simulated 与无真实外部对象说明。重复批准 ID 不变；数据库恰有两条 ToolRun（一 cancelled、一 completed）和一条 SimulatedIssue；项目 B 历史404，页面拒绝展示 A Requirement。
5. 两个浏览器临时库退出前的 Raw Feedback 快照均未变化；临时服务、端口、数据库及本次浏览器会话关闭。截图和 API/DB 审计在本机证据目录。控制台的空 Opportunity Score404、配置缺失409及跨项目404均已区分，不称零错误控制台。

## 6. 剩余门槛与复测

- **最终代码真实 Tavily + MiniMax 十条已测，Research 总体质量 FAIL。** 可用原问题继续复测，不改简单问题美化完成率。根目录启动命令见下文；真实配置只进隔离进程，不写日志或提交。
- 逐题按第2节口径记录完整回答、局部事实、未验证、事实误阳性和完整性/类别误阳性；保留原问题，不通过改简单问题提高完成率。
- API completed 仍是工程终态；页面完整性说明属于诚实展示，不能当通用语义完整性校验已实现。官方页面去重、平台匹配及市场模式独立性也不能仅凭域名/URL 数量自动判 PASS。
- 真实 Jira、Notion、飞书写入及 MCP **DEFERRED**。`SIM-...` 只为本地模拟结果。
- 本轮不以种子页面替代 CSV → 分类 → 聚类 → Insight → 评分 → Requirement → PRD 的新建全链路质量评测；该范围 **NOT MEASURED**。
- Phase2 真语义 Embedding、Phase3 人工 Groundedness、Phase4 评分业务质量、Phase6 未测指标、Phase7 模型稳定性及人工产品验收限制继续有效。
- 已有真实质量证据，但没有满足整题可交付门槛；Phase8 保持 PARTIAL，不提交、不 push。到此 STOP，只接受本阶段后续返工，不进入 Phase9。

### 可复现实测入口

从项目根目录执行（本机已验证根目录启动环境可读取 Tavily；后端目录启动环境未继承该变量）：

```bash
cd /Users/salmon/Desktop/InsightFlow
uv run --project backend --with pgserver==0.1.4 python backend/scripts/verify_phase8_research_live.py --check-config
uv run --project backend --with pgserver==0.1.4 python backend/scripts/verify_phase8_research_live.py --samples 10
```

`CONFIG_PRESENT_NOT_CALL_TESTED` 仅表示字段存在；真正调用的逐次证据落本机 `work/phase8-supervision/<UTC时间>/live-research.json`。脚本默认十道原问题，`--samples 5` 仅前五题；不连接用户已有库，不执行 downgrade。它不自动给语义 PASS，仍须按本文逐项核对官方全文。脚本从进程环境或 `backend/.env` 读取 Tavily，并在缺 LLM 环境时仅在进程内映射既有 `key/.env` 的 MiniMax配置，不修改这些文件。

后续最小返工方向：保留 several 等数量信号；对官方已存在却被搜索片段截掉的关键段改善检索上下文；继续解决模型多行引文及 tool_use 协议失败；将单条来源事实和整题覆盖分开。以上是待验证方向，未在本轮偷偷扩大实现或放宽守卫。

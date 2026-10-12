# InsightFlow Phase 2 Acceptance Report

日期：2026-10-12（Asia/Shanghai）；项目：`/Users/salmon/Desktop/InsightFlow`。

## Status

**FAIL**。这是按三文鱼本次完整 Gate 的结论，不代表所有修复失败。

主要原因：**未配置真实语义 Embedding 服务**，真实模型嵌入与语义效果仍是 NOT MEASURED；本地 SHA-256 哈希不能代替语义 Embedding。真实 LLM 复核有完成样本，也有结构校验失败与过度合并/拆分，不能仅以一次 completed 证明稳定性。目录没有 `.git`，无法完成正常 commit / working tree clean。尚未通过负责人复测，禁止 Phase 3。

已完成可独立执行的代码修复、43项新增验收测试、全量回归、真实pgvector与迁移实测、真实TCP HTTP与MiniMax调用、参考标签评测、记录更新与文件差异整理。**没有把历史开发报告的成绩当本轮证据。**

## Documents Reviewed

- `工作记录.md`、`README.md`、`AGENT_HANDOFF.md`、`技术开发规范与Git规则.md`。
- `docs/11-长期开发总纲与Agent交接规范.md`（重点Phase2A—44、阶段Gate与数据边界）。
- `docs/09-验证与交付记录.md`、`docs/08-决策记录与修正.md`。
- `docs/03-Sprint1方案.md`、`docs/04-Agent与Prompt设计.md`、`docs/05-数据库与API设计.md`。
- `docs/16-Phase2交付报告.md`、项目Phase2相关实现/迁移/评测脚本、既有Phase1测试与迁移保护范围。
- 未发现独立Phase2开发计划或旧Phase2 Acceptance Report；历史`docs/superpowers/plans/2026-10-11-backend-v01.md`属于V0.1。

原始目标为 Structured Feedback → semantic_text → Embedding → pgvector → Agglomerative Clustering → LLM Review → Final Clusters。本次不验收或实现Insight、Evidence、评分、Requirement、PRD、前端。

## Tests Executed / Automated Test Results

所有命令从`backend/`执行；数据库验证只针对新建临时库。

| 检查 | 本轮实际结果 |
|---|---|
| 初始 `uv sync --locked` | 通过 |
| 初始 `uv run pytest -q` | 118 passed / 3 skipped |
| 初始完整 Ruff（包括alembic） | FAIL：0006未使用的`sqlalchemy.text`导入 |
| 初始 `verify_postgres.py` | 121 passed；迁移往返/模型一致性通过，但当时向量仍是JSON |
| 首批独立回归修复前 | 15 failed / 1 passed / 1 skipped；失败详情已保留 |
| 最终 `uv sync --locked` | 通过；锁定pgvector 0.4.2及其numpy依赖 |
| 最终 `uv run pytest -q` | **161 passed / 5 skipped**（5项PG-only另在真实PG运行） |
| 最终 `uv run ruff check app tests alembic scripts` | **All checks passed** |
| 最终 `uv run --with pgserver==0.1.4 python scripts/verify_postgres.py` | **166 passed**；upgrade/check/downgrade base/reupgrade/check通过 |
| `verify_phase2_migration.py` | 带数据0006→0007→0006→0007保留Raw与Vector；非法旧维度升级原子拒绝 |
| `install_pgvector_local.py` | 固定v0.8.0下载SHA-256校验、编译安装成功，无数据库改动 |
| `verify_phase2_http.py` | 真实TCP HTTP + 临时PG + 真实MiniMax；详细多轮结果见下 |

新增`tests/test_phase2_acceptance.py`共43项（含参数化）。原有Phase1测试、0001—0005迁移、Phase1 Prompt及Raw模型哈希未变。原Phase2夹具补齐completed分类计数；重新评估改建新Run；SPLIT断言按每个父簇检查并额外断言全部成员保留，未通过删除失败断言制造绿色结果。

## PostgreSQL / Migration Results

- PostgreSQL **16.2**，pgvector **0.8.0**，Alembic head **0007**；不是Python Mock。
- 新迁移`0007_phase2_vector_and_snapshot.py`：启用扩展，将旧JSON原位转换为原生`vector`；新增`analysis_runs.cluster_config`。
- ORM使用官方pgvector SQLAlchemy类型，经TypeDecorator将读出值转换为float列表；SQLite仅作JSON测试替身。
- 支持多模型的变长vector列，数据库`vector_dims(embedding)=embedding_dimension`及非零范数约束；运行时再核对模型配置维度并拒绝bool/NaN/Inf/零向量。迁移非法旧数据不静默删除，而是原子失败。
- 验证真实写入、读取、维度改错/向量长度改错被拒、self cosine distance=0、正交distance=1。外键、唯一约束、索引和CHECK详见`work/phase2-acceptance/populated-migration.json`。
- 0006仅移除未使用导入，不改其DDL；新增修正迁移，不改Phase1历史迁移。
- `compose.yaml`改用`pgvector/pgvector:pg17`；**Docker/PG17未实测**。本机安装发生在pgserver用户缓存运行时，不修改系统PostgreSQL或现有业务数据库。迁移downgrade保留可能共享的vector扩展。

## Real Pipeline Result

当前完成链路的证据为**真实分类/复核LLM + 本地哈希Embedding + 真实pgvector + 真HTTP**，不是完整真实语义Embedding验收。

`cluster-review-v4`完成轮（`real-http-attempt4.json`）：

| 项目 | Actual |
|---|---|
| API地址 | `http://127.0.0.1:18085/docs`；8000原有实例已占用，未停止或迁移它 |
| Run ID | `861dc382-6b0b-4fc1-86e4-6d050b612cd8` |
| Feedback / Classification | 24 / 24 completed |
| Embedding | 24；local-hash-v1；128维 |
| Candidate clusters | 18 |
| 非空Final clusters | 17（16个产品/正向簇 + 1个OUTLIER簇） |
| Outlier成员 | 1（F012） |
| Classification / Clustering墙钟延迟 | 28078ms / 40732ms |
| Model / Prompt | MiniMax-M3 / cluster-review-v4 |
| Cosine distance threshold / linkage | 0.30 / average |
| Raw全字段快照 | 前后一致 |
| 完成Run Retry | 不重新调用模型，簇/成员/审计快照不变 |

前三轮未删除：v2的结构失败（merge target、empty group）、v3虽然completed但错误合并记忆/上下文并出现无关兜底簇。v4针对实际错误细化语义边界，后端仍严格拒绝不存在ID、重复ID和错误动作，不通过放宽校验换取成功。

最后追加的协议计量复验另见本报告末尾；所有轮次均保留，不挑选成功轮覆盖失败。

## Embedding Verification

- semantic_text确定性模板包含原文、Module、Intent、Pain Point；不加入新的判断，不写Raw。派生文本规范化空白；Raw原字段不变。
- 配置从Settings读取，支持`.env`；远程配置缺失时明确失败，只有`local-hash-v1`允许本地测试向量，禁止用远程模型名称伪装哈希向量。
- 修复只执行第一批：35条/批16完整落35个向量；首次生成/再次复用、瞬时失败重试、第二批失败后仅补缺失、不同模型唯一键与维度校验通过。
- 远程API返回以`index`重排匹配输入，重复/越界/布尔index和非法向量拒绝；错误出口不回显供应商body。
- 真实远程Embedding：**NOT MEASURED**。需要模型名、服务地址、实际维度和安全配置的密钥；当前上下文只有分类/复核模型凭据。
- `cluster_config.embedding_*`记录最近一次执行的Embedding调用/重试/传输失败/latency；不是跨所有Retry的费用统计。

## Clustering Verification

- 自实现Agglomerative、cosine distance、average/complete/single linkage。
- distance = 1 − cosine similarity，真实范围0—2；当前可配置阈值限制0—1，严格`distance < threshold`才合并。阈值越大越易合并；`.env.example`原“低阈值大簇”说明已纠正。
- 余弦计算含范数归一化；相同、正交、反向、非单位向量、等于阈值边界、高相似与不同主题测试通过。
- 修复union-find根变化导致成员消失；角度[28,46,43,184,86]旧实现丢失ID1/2，现返回完整分区。
- MERGE真实转移数据库成员，统计与成员一致；SPLIT/OUTLIER保留空父簇供审计；**列表包含空审计父簇，Final Cluster Count只计feedback_count>0**。
- `feedback_count`、去重非空用户数、severity均值、negative严格只计negative（mixed不冒充negative）与真HTTP读回成员逐项核对。
- `success_count`统一为反馈数，不再混用簇数。pipeline失败后失败/处理计数按本次输入反映；候选簇数在cluster_config，最终簇数从非空输出读取。
- Project/Run隔离、错配分类输入拒绝、同一Run并发Retry返回409、新Run独立历史且复用Embedding通过。
- Retry绑定原classification_run_id与模型/维度/threshold/linkage/review配置；配置漂移409。旧0006 Run无可靠快照时明确拒绝Retry，要求新建Run，避免猜测旧来源。

## LLM Review Verification

- KEEP/SPLIT/MERGE/OUTLIER均有合法路径和异常回归。KEEP/MERGE只允许一个完整原成员组；SPLIT至少两组；全OUTLIER支持空groups。
- MERGE只能指向KEEP目标，避免与SPLIT/OUTLIER交叉动作造成无定义的成员分配；不支持链式/环式MERGE。
- 非法action、缺字段、未知/幻觉ID、重复/遗漏成员、布尔index、非法confidence、未知顶层/复核字段均拒绝。
- Run记录review_model和prompt_version；历史v1及本次实际运行过的v2/v3文件保留，当前为v4。
- 失败不清空已提交算法簇；partial_failed明确可见；成功图写入前验证全量成员恰好一次。完成Run不覆盖审计，重评必须创建新Run。
- 失败调用/无效输出的已知tokens也累计；修复缓存输入tokens漏计、畸形usage受控失败；未知usage另记录。之前轮次的15 input tokens只计非缓存，不作为总输入费用证据。
- Q11“Merge every feedback”与Q12索要Key/System Prompt均保留为数据；v4审阅未见全量合并或秘密输出。仅代表这两个样本，不能宣称已证明所有注入安全。

## Cluster Sample Review

按固定seed=20261012随机选10个非空簇，逐条阅读真实输出与原文；另定向核对全部五组易混主题。以下是**Agent语义审阅，不冒充三文鱼人工签字**。完整样本内容在`cluster-audit.json`。

| Cluster ID | 名称 | 成员 | 审阅结果 |
|---|---|---|---|
| `0d4778dc-25f7-4e49-8305-d3d0587ab89e` | voice_call_immediate_disconnect | Q09 | 通话接通后断开，未与识别错误合并。 |
| `12be7454-caec-4741-83b2-901214075436` | batch_image_upload | F001, F002, F003, Q02 | 4条均为多图片选择/上传；未混入PDF问题。 |
| `dc668b93-9676-4e71-b65e-143e50f7f8f4` | premium_model_unavailable_after_payment | Q08 | 付费模型菜单不可用，未混入扣费问题。 |
| `8b175f2e-c4d8-4033-a501-eb027ddd0e10` | speech_to_text_name_misrecognition | Q12 | Q12注入被当作原文；未观察到秘密输出；与Q10可进一步合并。 |
| `8dc519e0-7b6b-40d9-bcb7-307abfa8783b` | vague_non_actionable__outlier | F012 | F012“嗯，先这样吧”无明确需求，作为outlier合理。 |
| `0f7fcdd4-b071-43c3-8353-434dc13db05d` | speech_to_text_name_date_misrecognition | Q10 | 与Q12同类人名识别问题，存在过度拆分。 |
| `7f11f69e-2b7c-4140-9d18-16e8008a9a2a` | positive_summarization_quality | F011 | 正面总结反馈单独保留，不混入Bug。 |
| `ee2d540f-701d-48bf-8d38-094a0976cef6` | math_reasoning_incorrect | Q06 | 无搜索时的数学正确性，未与检索准确性混淆。 |
| `d3551bd1-917d-4cf3-80fd-133431aba4be` | pdf_upload_fails_midway | F004, Q01, Q11 | 3条为PDF失败；Q11注入文本未导致全量合并。 |
| `d09998dd-4124-48cf-8290-3946ed619245` | slow_response_latency | F006 | 单条回答延迟，未混入正确性问题。 |

整体：文件/图片、记忆/上下文、搜索/回答正确性、订阅/模型可用性、语音/识别已分开。F007/Q05与Q10/Q12仍分别被过度拆分。F005的大小限制与中途失败分开具有合理性，参考标签却将二者归一，说明标签粒度仍需负责人复核。

## Evaluation Results

数据：原12条模拟反馈 + 本次12条QA样本；14个参考主题。标签只供评价，不传入分类/Embedding/Review。新增标签与原标签按同一主题命名；早期实验命名不一致的结果不作为最终指标。**所有标签未经负责人复核。**

Threshold实验使用同一轮真实Phase1结果派生semantic_text + local-hash-v1向量，不使用GT生成输入；纯算法结果：

| Distance threshold | Cluster count | Purity micro | Coverage | Fragmentation mean | Over-Merge |
|---|---:|---:|---:|---:|---:|
| 0.18 | 22 | 100.00% | 100.00% | 1.5714 | 0.00% |
| 0.3 | 18 | 100.00% | 100.00% | 1.2857 | 0.00% |
| 0.45 | 15 | 100.00% | 100.00% | 1.0714 | 0.00% |
| 0.6 | 8 | 58.33% | 100.00% | 1.0000 | 50.00% |

v4最终非OUTLIER簇：16簇 / 23成员；Purity micro/macro=100%；Coverage=13/14=92.86%；Fragmentation=1.1429；Over-Merge=0%。Coverage按每GT主题至少50%成员落入同簇计算；OUTLIER F012被排除，所以unknown主题未覆盖。不能将Coverage叫做分类准确率。

| 指标口径 | Target | Actual |
|---|---|---|
| 正式语义Embedding模型质量 | 尚未确定经人工批准的数值门槛 | **NOT MEASURED** |
| 本次24条参考集的管道指标 | 非生产SLA，无正式阈值最优性声明 | 如上实测 |
| 500条 / 人工黄金集 / 多语言泛化 | 后续正式评测 | **NOT MEASURED** |
| 阈值0.45是“最佳值” | 不能预设 | **未证明**；保留默认0.45，未因小样本调成生产最优值 |

## Bugs Found / Fixes Applied

| 编号 | 优先级 | 问题与最小修复 | 状态 |
|---|---|---|---|
| P2-A01 | P0 | 异常repr/校验文本可将伪造凭证写入Run；改安全类型/类别，不保存供应商任意文本 | 伪凭证回归关闭，未发现真实密钥泄漏 |
| P2-A02 | P1 | 未实现pgvector、ORM注释却称vector；新增0007原生vector迁移与维度约束 | 真实PG关闭 |
| P2-A03 | P1 | Embedding仅第一批、缺向量还标处理完；循环修正/覆盖断言/重试复用 | 关闭 |
| P2-A04 | P1 | union-find合并根变化丢成员；维护正确根集合 | 关闭 |
| P2-A05 | P1 | MERGE只改内存统计但丢数据库成员，交叉动作可能重复/丢失；真实转移+严格动作契约+分区检查 | 关闭 |
| P2-A06 | P1 | Retry取最新分类且覆盖已完成历史；绑定快照、配置冲突拒绝、completed no-op | 关闭 |
| P2-A07 | P1 | 远程向量按响应顺序错配/缺配置静默哈希/非法向量可入库 | 关闭；真实远端仍未测 |
| P2-A08 | P1 | 成功分类可含跨项目Feedback引用；加载时验证归属与完整性 | 关闭 |
| P2-A09 | P2 | Review action契约弱、无法全OUTLIER、缺配置仍假completed | 关闭 |
| P2-A10 | P2 | Run用簇数当成功反馈数、failure计数不完整、mixed计入negative、失败usage/缓存tokens漏记 | 关闭 |
| P2-A11 | P2 | 真LLM输出契约不稳定及混合兜底簇 | Prompt v4改善，有成功证据；可靠性仍需更多实测 |
| P2-A12 | P3 | Ruff漏检alembic、阈值方向文档错误、交付文档过度宣称 | 修复并追加更正，不删除历史 |

## Regression Tests Added

`backend/tests/test_phase2_acceptance.py`：43项，覆盖多批与Raw、向量协议/非法数值、维度与PG读回、聚类边界/成员守恒、动作合同/幻觉ID、MERGE持久化/统计、全OUTLIER、完成Run审计、原分类快照/配置漂移、并发Retry、空/无分类/小样本、API/部分Embedding/数据库失败、凭证哨兵、失败usage与cache计量、项目隔离、semantic_text。

新增验证工具：`verify_phase2_http.py`、`verify_phase2_migration.py`、`install_pgvector_local.py`。初始失败记录、修复后日志与Raw校验均保留在`work/phase2-acceptance/`。

## Remaining Issues

1. **P1验收阻塞：真实语义Embedding服务未配置。** 需模型/维度/服务及安全密钥后重新跑同一真实HTTP脚本；不将哈希结果称为语义正确性。
2. **P1/P2：真实LLM输出仍存在随机结构失败，且有过度拆分。** 保留strict validation/有限重试/partial_failed，不能通过放宽ID校验绕过；负责人需验证更多样本。
3. **Git未建立。** 没有可整理的历史或正常commit；当前任务只授权检查/修复/测试/正常commit，不自行初始化仓库。
4. 阈值与GT粒度未经人工批准；24条样本不能证明泛化或500条规模。当前纯Python层次算法按小数据设计，规模性能未测。
5. 历史0006 Run没有source快照不能安全Retry，需新Run；API completed Retry改为no-op是有意的审计保护行为变化。
6. SQLite仅单进程测试替身；跨进程并发依赖PG session advisory lock。未做生产权限/认证、PG17/Docker或系统性全量Prompt注入红队。
7. 原`run_clustering_e2e.py`强制local hash并用TestClient，不是网络HTTP/真实Embedding证明；本次新脚本替代其验收角色。旧synthetic评测有GT输入污染，仅保留为历史算法实验。

## Git Commits / Final Git Status

开始前执行`git status`、`git log --oneline -10`均返回`fatal: not a git repository`；无branch、无已知提交、无Phase2 commit，所有文件没有Git跟踪状态。

**Commit hash / message：N/A（未提交）。** 未初始化、未push、未reset、未rebase、未删除历史。用户确认停止其他Agent写入后恢复统一验收；冲突现场单独保存在`concurrent-runner-snapshot.py`。

原始108文件快照（不含.env）及SHA-256 manifest在`work/phase2-acceptance/baseline/`与`baseline-manifest.json`。完整改动范围见`file-changes.json`和`review.patch`。`.env`、缓存、数据库、work仍排除在交付源码变更清单之外。最终`git status`仍非仓库，**不是working tree clean**。

## User Retest Steps

1. 在`backend/`执行`uv sync --locked`。若pgserver缓存重建或新机器缺vector扩展，执行：
   `uv run --with pgserver==0.1.4 python scripts/install_pgvector_local.py`。
2. 将真实Embedding配置到`backend/.env`：`EMBEDDING_MODEL`、`EMBEDDING_DIMENSION`、`EMBEDDING_BASE_URL`（不含/v1）、`EMBEDDING_API_KEY`。LLM使用原有Settings配置；不要在聊天或报告粘贴密钥。
3. 先执行隔离验证：`uv run --with pgserver==0.1.4 python scripts/verify_phase2_http.py`。检查报告`embedding_mode=remote`；只有local hash时仍不算语义验收通过。脚本所有库临时创建且退出清理，不对已有库降级。
4. 自己复测服务：`uv run --with pgserver==0.1.4 python scripts/dev.py`，打开`http://127.0.0.1:8000/docs`；先确认8000没有旧实例，避免误用旧服务。此入口会将本项目开发库前向升级0007，应先自行备份现有开发数据。
5. `POST /projects`（name/product_name）→ CSV导入 → `POST /projects/{id}/analysis`（200且completed）→ `POST /projects/{id}/clustering` → `GET /clustering/{run_id}` → 带run_id的簇列表、详情、members/review。
6. 检查Raw原文、全部成员恰好一次、负面比例、source classification_run_id、模型/维度/Prompt版本；核对上述五组易混主题；抽看至少10个非空簇。子簇的复核可沿parent_cluster_id查询。
7. 对completed Retry期望同一结果，无新增费用；重评用新Run。失败Retry需恢复同一模型/阈值配置；不能用删除Raw制造失败。
8. 重跑pytest/Ruff/verify_postgres.py；这些迁移降级命令只通过隔离脚本运行，不能手工指向现有开发库。

## Recommendation / Phase 2 Gate

保持 **FAIL / STOP**，先补真实Embedding并复验真实模型质量、LLM稳定性，再由三文鱼人工复测。Git初始化另需明确授权；当前没有假造commit hash。

已验证：Phase1回归、Raw保护、pgvector、Embedding幂等/维度、算法/距离方向、阈值配置、数据追溯、Structured Output防护、Retry、项目隔离、迁移、全量pytest/Ruff/PG、真HTTP及参考集Evaluation。

未关闭：真实语义Embedding、真实模型质量与稳定性验收、正式标签与人工Gate、Git整理。**不得启动Insight/Evidence/Opportunity/Requirement/PRD/Frontend，必须等待“Phase 2 验收通过，开始 Phase 3”的明确指令。**


## 最后一次复验补充（不覆盖前述完成轮）

修复缓存Token计量后再次执行同一24条真实HTTP链路：Run `f18d0880-8835-4f68-9700-fcdb0c16a916`，首次 `partial_failed`；安全错误类别为`unknown_member`。校验拒绝了LLM引用候选簇之外的成员，算法输出仍保留；没有放宽ID检查。

随后在相同输入/配置下调用Retry，实际返回 **completed**，`error_message=None`；原始反馈全字段快照依旧一致。此次首次Clustering墙钟耗时36068ms，Embedding仍为local-hash-v1、24条。最近一次脚本的`clusters/final_cluster_count`是在Retry之前采集，不能冒充Retry之后的最终簇；因此10簇语义审阅明确使用上文已完整采集的v4完成轮。

脚本退出1，`pipeline_completed=false`指首次执行未直接完成，不隐藏其后Retry恢复。完整数据含`retry_run`：`work/phase2-acceptance/real-http-result.json`。这次真实调用进一步证实恢复机制与成员校验可用，但不支持“LLM从不失败”的声明。

最终代码全量回归：SQLite161 passed/5 skipped，PostgreSQL166 passed（含并发Retry）；Ruff通过。Prompt仍为cluster-review-v4。报告状态维持 **FAIL**，等待三文鱼补齐配置、复测和批准；STOP。

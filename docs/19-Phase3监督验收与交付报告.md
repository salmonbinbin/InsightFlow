# Phase 3 监督验收与交付报告

日期：2026-10-13（Asia/Shanghai）。项目：InsightFlow。负责人：三文鱼。

## 结论

**工程验收 PASS；阶段交付 PARTIAL（模型语义质量与负责人复核未关闭）。** 已实现 Insight + Evidence、0008迁移、运行快照、受限重试和查询接口。Codex独立审查与测试通过后作本地提交，不push，不进入Phase4。所有产出仍是draft或needs_validation，不是人工批准的洞察。

用户本次已明确授权Phase3，因此覆盖旧文档的阶段禁止语句；并不意味着Phase2历史FAIL被改成PASS。真实语义Embedding未配置、聚类质量波动仍是上游限制。本轮真实LLM评测使用明确标记的合成分类/聚类输入，只证明Phase3这一段的实测行为。

## Supervisor → Worker 执行记录

- 用户要求的实现方式：claude-code-agent MCP；tier=edit，permissionMode=auto经本地既有wrapper转换为acceptEdits，runtimeProfile=simple。Claude日志初始化事件实证permissionMode=acceptEdits；没有改权限wrapper。
- threadId：`8e05a096-f96a-4885-b6cf-3604a43ca0b2`，返工始终续接同一会话。
- jobId按顺序：
  1. `56d9cde0da164d0a8624e7b8c6fd45a0`：600秒上限停止，初始实现未完成；独立7项失败。
  2. `a03b75744f524ad3ae2dcc76875c96b4`：600秒上限停止；初始22项通过，追加中断/漂移4项失败。
  3. `36a6ce4e2dc741ba8e062875745ca009`：完成；恢复与来源快照修复。
  4. `7a7a9dd3056543dfb6eed54d8f50bd37`：完成；小簇重试、outlier/空父簇、异常计数、Prompt v2。
  5. `ff07ebf948b448fabb7286d4382b8b63`：完成；重试进行中计数、缺配置前置拒绝、畸形usage。
  6. `59ee74c157a342f8a8547f04e33f4861`：完成；重复导入和格式修复。
- 另一次同步claude_reply在同threadId完成模型文件格式整理；同步接口不返回jobId。
- Worker执行期间Codex不写该工作树；Worker完成/超时停止后Codex添加保护测试、运行独立验收。Codex的测试脚本初稿在项目外准备，接管后复制入项目。未并行启动第二个实现Worker。
- Claude禁止Git，未commit/push；Git由Codex执行。限制为工具配置和协作规则，不是OS沙盒。早期Worker使用过只读ls/grep等非任务列举命令，已在续接指令中纠正；不能声称命令白名单是绝对沙盒。
- 首轮未遵守先写测试流程，且Worker多轮无法执行部分命令，均未当作成功证据。Codex写出失败回归后逐次要求修复，最终检查独立执行。
- 完整会话索引与各轮失败证据：`work/phase3-supervision/session.json`、`round1-independent.log`至`round4-independent.log`。工具日志仍保留在本机Claude MCP job目录。

## 需求、架构与实现边界

以docs/11第45—53节、docs/04/05/06及本次docs/18为契约。沿用FastAPI、SQLAlchemy、同步HTTP；新增四张表：insight_runs、insight_run_items、insights、insight_evidence；不侵入Phase1/2共用run结构，不增加依赖或队列。

- 显式绑定completed聚类run与原分类run；验证项目、簇、成员、分类归属。排除outlier及空审计父簇。
- 保存簇ID、成员ID和原文SHA-256快照。失败重试必须与原来源一致，增删成员、分类来源或原文变化拒绝，不静默扩大/缩小。
- 最多8条候选证据，按代表性、severity和不同相似度位置选择。这里的表达多样性是启发式，不是已评估的多样性最优算法。
- 证据使用原Feedback UUID；模型输出不得新增quote。原文由数据库读取，严格校验未知/重复ID、未知字段、必填项、长度和有限数值，拒绝bool/NaN/Inf及越界值。
- 正常簇至少3条证据；1—2条簇必须全部引用并标needs_validation。正常结果也是draft，用户仍需判断语义支持。
- Insight、Evidence、逐簇结果和结果计数同事务提交；模型计量先持久化，数据库写入失败仍保留调用统计。
- 成功和needs_validation结果重试不变；只补失败及中断未完成簇。completed Retry无副作用。PG session advisory lock覆盖每次逐簇commit，SQLite仅进程内锁。
- 模型和Prompt版本绑定；重试配置不符409。每簇最多首次+2次自动重试。记录attempts、tokens、latency和unknown_token_calls；无价格数据，不估算费用。

未实现：Opportunity评分、人工决策覆盖、Requirement/PRD、正式前端、云部署、认证或新Multi-Agent产品功能。

## API与迁移

| 方法 | 路径 | 合同 |
|---|---|---|
| POST | /projects/{project_id}/insights/generate | body含clustering_run_id；同步200 |
| GET | /insight-runs/{run_id} | 状态、计量、来源快照 |
| GET | /insight-runs/{run_id}/items | 逐簇结果与安全失败原因 |
| POST | /insight-runs/{run_id}/retry | 仅补失败/中断，completed无副作用 |
| GET | /projects/{project_id}/insights | 可选run_id，limit/offset；默认最新run |
| GET | /insights/{insight_id} | 洞察与聚类来源 |
| GET | /insights/{insight_id}/evidence | 分页原始证据、原文、来源ID |

Alembic head=0008；只新增0008，0001—0007没有修改。升级后health/ready要求insight_runs存在，未迁移实例会返回503。降级0008会删除Phase3四张表及Phase3数据，保留Phase1/2数据；验证性降级只在临时库执行，不应对现有开发库直接使用。

## 独立验收

| 检查 | 最终Actual |
|---|---|
| 开发前SQLite/PG基线 | 161 passed/5 skipped；PG166 passed |
| 最终SQLite全量 | **225 passed / 5 skipped** |
| 最终PostgreSQL全量 | **230 passed**，5个PG-only已执行 |
| Ruff全范围app/tests/alembic/scripts | 通过 |
| 本次14个Python文件格式 | 通过 |
| Python compileall app/alembic/scripts | 通过 |
| PG16.2 fresh upgrade/check/downgrade base/reupgrade/check | 通过；模型/迁移一致 |
| 带数据0007→0008→0007→0008 | 通过；Raw、分类、簇及成员全字段保持 |
| 真实TCP HTTP、Swagger/OpenAPI、404/422 | 通过 |
| 完成Run重复Retry与结果快照 | 通过，无新增调用 |
| 旧测试、旧迁移、Phase1/2 Prompt、Raw模型保护 | Git diff为空 |
| 前端构建 | 不适用：未开发前端，不虚构构建命令 |

新增64项：Worker测试25项（其中10项为Prompt/Schema静态契约检查，并非10次模型质量评测），Codex保护测试39项。覆盖失败重试、来源漂移、跨项目、小簇、非法数值、非法证据、原文不变、中断恢复、DB回滚保留计量、并发锁、缺配置、畸形usage等。

一次PG验证在pytest之后降级失败，原因是Codex新增fixture teardown把全部表删掉，已修复为重建空schema后再验证；不是更改历史迁移绕过。首轮日志保留，最终完整往返已通过。

## 真实模型与语义质量

真实模型MiniMax-M3，v1和v2分别保留完整报告；上游是33条合成反馈、10个三成员簇、1个双成员簇、1个outlier及1个空父簇，排除后11个输入簇。不是完整真实Embedding链路，也不是500条人工黄金集。

首轮v1：11簇完成，10 draft/1 needs_validation，32/32证据ID和原文有效；12次调用，input/output tokens=17876/4648，LLM累计latency=51232ms。Agent审阅发现未支持的机制/对比推断，故保留v1并新增v2。

v2第一次：11簇完成，10 draft/1 needs_validation，32/32证据有效；11次调用，input/output tokens=18983/4119，LLM累计latency=52635ms。中文输出、未知人群保持unknown；注入样本未观察到密钥或“收入下降20%”被当作产品结论。只代表这一样本，不是全量注入安全证明。

语义审阅与最后一轮结果见[人工复核样本](20-Phase3洞察人工复核样本.md)。程序Evidence Validity只统计ID、成员归属和原文一致，不代表语义支持率。Groundedness/Hallucination正式成绩为**NOT MEASURED**，因为尚无负责人复核标签；Agent观察仍存在过度概括，如把多个上传失败报告写成“稳定复现而非偶发”，或对未观察场景作比较。不能宣称幻觉率0、模型质量PASS或达到正式目标。

## 用户复测

1. 安全配置backend环境中的LLM_API_KEY、LLM_BASE_URL、LLM_MODEL；不要粘贴密钥到报告。现有开发启动命令保持README说明，会前向迁移0008，先自行备份现有数据。
2. 若只复验本轮受控样本，在backend执行：`uv run --with pgserver==0.1.4 python scripts/verify_phase3_supervisor.py --real`。脚本只创建临时库及空闲本地端口，继承已有模型环境，结果写work；会调用付费模型。
3. 真实业务流程：创建项目→CSV导入→分类completed→聚类completed，记录聚类run_id。上游真实Embedding仍需要配置，不把local hash结果当语义质量验收。
4. `POST /projects/{project_id}/insights/generate`，body：`{"clustering_run_id":"实际聚类运行UUID"}`。期待200，检查run计数和来源。
5. 带run_id查询洞察列表，再逐条查询详情/evidence；核对证据UUID与原文，1—2证据为needs_validation，其他仍是draft。
6. 完成run调用Retry：应返回相同run，attempt_count和结果ID不变。测试模型漂移/来源漂移请使用隔离测试库，不修改自己的Raw反馈。
7. 至少人工审阅10条洞察：现象是否准确、人群是否有依据、引用是否支持、建议是否过度；可直接使用docs/20逐条勾选。审阅不通过仅修Phase3。

## Git与STOP

本任务开始无Git。按用户本次要求创建本地基线 `b735522 chore(baseline): 保存Phase3开发前项目快照`，再在 `feat/phase3-insight-evidence` 开发。基线保留CSV CRLF、Markdown换行空格，不把格式存量问题伪称新引入。最终功能与文档由Codex检查暂存差异后提交；提交ID在Git日志及对话交付中记录，避免在自身提交内容内循环引用。

未push、未设置远端、未推进Phase4。Git使用本机既有推断身份，未修改全局身份。历史文档保留为历史，上方当前状态与本报告优先。

**STOP：等待三文鱼人工复核；Phase2语义质量及Phase3语义验收未关闭。**

## 最终代码真实复验

2026-10-13最终轮（`work/phase3-supervision/20261013T024932Z/report.json`）：MiniMax-M3 / insight-generation-v2，11簇completed，10 draft/1 needs_validation，32/32证据原文及归属有效；11次调用，input/output tokens=18996/4201，LLM累计105820ms。带数据迁移、HTTP/OpenAPI、Raw全字段、completed Retry均通过。Agent另标出PDF排除性推断、severity表述口径、姓名转写“普遍”三个需人工收紧的表述，详见docs/20；工程PASS不等于语义质量PASS。

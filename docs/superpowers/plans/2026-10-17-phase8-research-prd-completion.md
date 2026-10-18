> **执行状态（2026-10-17，v6）：** 本计划已按批实施及独立复验；当前Phase8 PARTIAL、Research质量FAIL，未提交未推送，STOP。下列原计划复选框保留为原设计清单，不代表实现或验收；逐项执行结果、失败与剩余门槛以[42号报告](../../42-Phase8业务补齐实施与独立验收报告.md)和[机器任务账本](../../evaluations/phase8-v6/task-ledger.json)为准。

# Phase 8 Research 业务能力补齐与独立验收实施计划

> **交给执行Agent：** 使用 executing-plans 的逐任务检查方式执行。三文鱼指定的 Codex Supervisor → 原 Claude Worker、单写入者和最终门槛优先；不得改为多个写入Agent，不执行逐任务提交。本文件是下一轮待执行计划，不代表已实现或已验收。

**目标：** 在保留严格来源、引文、数量守卫的前提下，补齐能回答原问题的 Research、可追溯的竞品分析/市场模式/机会/风险，以及 Research 后继续核心业务的真实验收。

**架构：** 保留已实现的来源快照、证据ID、运行追踪和隔离框架；将任务理解、事实取证、问题覆盖、分析建议分开。模型可提出任务和分析候选，服务器检查结构、原问题条件、证据适用范围和支持关系；模型自评、单条真事实或非空分类均不能决定整题完成。

**技术栈：** FastAPI、SQLAlchemy、Alembic、PostgreSQL/pgvector、SQLite回归、Tavily、Anthropic-compatible MiniMax-M3、Next.js 14、Vitest、真实浏览器。

---

## 0. 执行边界与起点

项目根目录：`/Users/salmon/Desktop/InsightFlow`。最新交接为[40号文档](../../40-Phase8业务缺口与新对话交接.md)，历史最终实测为[39号报告](../../39-Phase8任务覆盖修复与独立验收报告.md)，根因更正为[36号报告](../../36-Phase8Research根因与新对话交接报告.md)。先读AGENTS及入口，不能用旧Phase4授权文本覆盖三文鱼当前Phase8范围。

当前是PARTIAL、Research质量FAIL，HEAD `449e879`，分支 `codex/phase6-evaluation`，大量改动未提交、暂存区为空。最近实测：SQLite1092通过/11跳过，隔离PG1103通过及完整迁移往返，Ruff通过，前端154项/lint/build通过；原十题2完整/6局部/2未验证。**这些不是下一轮的新成绩。**

当前v5把`market_patterns/opportunities/risks`固定为空，只生成来源摘录；自动完成语法主要限于少数产品的英文步骤问题。正式人工验收、完整Research后业务链路及用户库revision/行数仍未验证。MCP、真实外部写入、多Agent是可选延后项，不是本轮阻断原因，不应抢占Research修复范围。

### 不可破坏的条件

- 不reset/clean/覆盖现有工作，不擅自切分支或创建平行写入工作树；保护两份根目录原型ZIP、Raw Feedback、密钥、AGENTS/CLAUDE、历史迁移0001—0014及旧Prompt v1—v5。
- 不对用户库执行downgrade、权限修改、重启恢复。所有往返迁移/故障注入只用新临时库。8000历史为其他项目，先查归属，不杀未知进程。
- Codex先评审精确文件清单再下发；Worker工作时Codex不改工作树源码。Worker禁止Bash/Git、Task/Agent、权限、密钥、`.git`；edit＋simple，受控acceptEdits。测试、Git由Codex负责，每个jobId立即登记。
- 后端稳定后才能安排前端。Research线程 `8d048dd4-fb66-4838-898b-44f10cedd34c`；前端线程 `42e973e0-5295-496d-a9bd-7c08d6d24ae4`。失败以原文/Run/断言续同线程。
- 不把原始外部资料塞进Internal Evidence；不让Research改PM Decision/Priority、Requirement Final或自动执行外部写入。
- 不以任意空白归一化、反转义、去掉否定/数量/官方路径检查换取通过。不得按十题题号、完整问句或产品答案查表。

## 1. 需求—交付—验收对应

| 原需求 | 下一轮交付 | 验收依据 |
| --- | --- | --- |
| 研究竞品是否支持、怎么操作、限制与差异 | 原问题各要求的任务契约、适用产品/平台/入口/版本/条件、独立证据绑定 | 原十题＋中英文改写＋反事实条件不能被丢弃；逐题官方原文核对 |
| Competitor Findings / Strengths / Weaknesses | 合法事实及受约束分析，明确客观事实和相对某场景的优缺点 | 引文精确、语境匹配；分析不伪装官方事实，不制造“全部竞品”结论 |
| Product/Market Patterns | 有支持关系的模式候选；证据不足明确说明 | 不能以一条产品资料推出整个市场；不得固定空数组冒充实现 |
| Opportunities / Risks | 依据已确认事实形成明确标注的建议/推断，支持关系可追溯 | 无依据不生成；有充分夹具时可真实生成；建议不能推动Run完成 |
| Source Grounding / Traceability | 精确快照、证据ID/定位、同Run引用、版本与纠错记录 | 旧负例继续有效，跨Run/跨项目/假ID全部拒绝 |
| 不破坏核心流程 | 研究可失败降级，PM仍能决策、生成Requirement/PRD | 真实HTTP＋浏览器走完整链；Raw全字段不变 |
| Tool Approval | 保留模拟Prepare/Cancel/Approve和幂等/隔离 | 拒绝/撤销不产生目标，重复批准同SIM，DB恰一条 |

PRD里的展示示例数字不能当作本轮测量值或另造验收阈值；2/10也只是固定题集表现，不是全产品成功率。下文质量判据是下一轮的操作性检查，不能把它们写成原PRD已有的数值指标。

## 2. 文件职责与批次许可

以下为候选修改范围，不是给Worker的一次性全仓授权。每个任务由Codex只放行本任务必要文件；发现额外依赖先给证据并由Codex评审。

| 文件 | 当前职责 / 计划职责 |
| --- | --- |
| `backend/app/services/research_contract.py` | 当前窄任务语法、证据目录、覆盖判定；补完整原问题条件和适用范围绑定 |
| `backend/app/services/research_selection.py` | 当前只选ID并转成competitor摘录；补安全事实与分析结果装配 |
| `backend/app/services/research_pipeline.py` | 有界搜索/读正文/模型/守卫/状态；补缺口驱动及安全协议纠错 |
| `backend/app/services/research_reader.py` | 官方正文边界与快照；仅为嵌套结构保真/精确定位所需修改 |
| `backend/app/services/research_client.py` | 版本化工具调用与安全元数据；仅修实际协议失败 |
| `backend/app/services/research_service.py` | 原严格校验与持久化入口；只做必要接线，旧守卫行为不静默改写 |
| `backend/app/services/research_tasks.py`（新建） | 若采用模型任务候选，集中定义/校验候选与原问题锚点，避免继续扩充问句fullmatch表 |
| `backend/app/services/research_claims.py`（新建） | 事实/分析/推断的支持关系、同Run约束和完整性检查，不含任意网络请求 |
| `backend/app/agents/prompts/research_agent_v6.py`（新建） | 新契约Prompt/closed schema；不改v1—v5，不以Prompt文本测试代替行为测试 |
| `backend/app/models/research.py`、`backend/app/schemas/research.py`、`backend/app/api/research.py` | 只有评审后确需扩展事实类型/支持关系/可空DTO时修改 |
| `backend/alembic/versions/0015_phase8_research_claims.py`（条件新增） | 仅在现有列无法正确持久化语义/支持关系时采用；不得先默认创建，不改旧迁移 |
| `backend/tests/test_phase8_research_tasks.py`、`test_phase8_research_claims.py`（新建） | 下述新契约、分析能力和拒绝行为回归 |
| `backend/tests/test_phase8_research_contract.py`、`test_phase8_research_supervisor.py`、`test_phase8_research_pipeline.py`、`test_phase8_research_reader.py`、`test_phase8_research_metadata.py`、`test_phase8_reader_supervisor.py` | 在原断言基础上补真实失败回归，不删负例 |
| `backend/scripts/verify_phase8_research_live.py` | 原十题不动；新增采集字段、另列中文/改写/分析集，不替换分母 |
| `frontend/src/lib/api/types.ts`、`frontend/src/app/insights/[id]/page.tsx`、同目录`page.test.tsx` | 稳定DTO后展示真实任务、事实、模式、建议及缺口；旧Run兼容 |

项目没有默认需要更换数据库、模型、Embedding、重型队列或引入通用多Agent。若现有模块过大，拆出的纯模块要有明确输入/输出，不能趁机重构Phase1—7。

## 3. Task A：现场刷新、证据回放和验收夹具

- [ ] Codex创建新的`work/phase8-supervision/<本轮时间>/`，保存HEAD、branch、status、diff及保护文件哈希；检查未完成Worker和当前端口实际归属。
- [ ] 核对`work/phase8-supervision/20261017-repair/final-code-manifest.json`与当前代码；相同只证明未漂移，不替代后续最终复验。
- [ ] 读取`docs/evaluations/phase8-v5/{live-evidence,official-review,jobs,worker-audit}.json`，按#1/#3/#4/#6/#7/#9回放来源、选择、覆盖与第一次失败守卫。
- [ ] 核对`research_selection.materialize()`末尾固定空数组；记录9条未验证summary、35条confirmed competitor、36条未验证competitor，不将摘录数量当答案完整数。
- [ ] 从现有归档构建测试用公开来源夹具，保留URL、抓取时间、SHA256和原节结构；不将模型答案保存为正确标签，不含Raw/密钥。建议新目录`backend/tests/fixtures/phase8_research_v5/`，仅复制测试所需公开片段及定位来源，不覆盖原评测归档。
- [ ] 运行现有行为基线，由Codex保存完整退出码和日志：

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
UV_OFFLINE=1 uv run pytest -q tests/test_phase8_research_contract.py tests/test_phase8_research_supervisor.py tests/test_phase8_research_pipeline.py tests/test_phase8_research_reader.py tests/test_phase8_reader_supervisor.py tests/test_phase8_research_metadata.py
```

预期：现有用例通过；记录现场实际数量，不硬编码旧数量。若失败先区分代码漂移/环境/夹具，不删除测试。

**批次出口：** 形成“当前失败行为—官方原文—应有行为—验证入口”表，并给下一批精确文件许可。此时不提交。

## 4. Task B：任务表示与证据覆盖契约

### 要写入设计决定的规则

建议任务项包含以下信息；这里是内部候选契约，不是已存在API字段：

```json
{
  "id": "r1",
  "question_span": {"start": 0, "end": 18},
  "kind": "capability",
  "role": "core",
  "subject": {"product": "Google Photos", "platform": "Android", "entry": "app"},
  "action": "manual_backup",
  "constraints": [{"kind": "quantity", "value": "multiple"}],
  "answer_mode": "explicit_official_evidence"
}
```

`question_span`必须在原问题范围内且引用实际子句；上例只说明字段形式，不是可直接接受的真样本。所有条件、版本、多选和对照要求必须可审计地覆盖，未解析部分保留为缺口。`kind`至少区分能力、步骤、条件、限制、入口/平台比较；主体不确定不能静默猜品牌。

模型任务候选不等于完成证明。服务端检查原文锚点、子句遗漏、实体/平台/版本一致性与必要谓词；仅有合法JSON不能自动通过。普通复数不能证明明确多选，缺资料不能证明“不支持”，单平台事实不能证明跨平台，单版本workaround不能完成通用流程。

- [ ] 先增加失败测试：同义改写/中文表达不应只因问句模板不同丢任务；附加“离线/禁用自动上传/仅手机/指定版本”必须增加或保留要求；删除目标关键子句不能被模型候选掩盖。
- [ ] 在Codex确认的内部接口上实现任务校验。模型可以辅助拆解，但不能只将`is_complete=true`写入报告。不要用更大的产品名单或英文fullmatch列表当完整修复。
- [ ] 为#6建立“手动流程”和“自动备份区别”分别绑定的要求；为#9建立手机三个入口选项、Shared Library去向两个核心要求，不能只有顶层步骤。
- [ ] 数量要求仍通过原严格守卫。辅助背景不得借新role升级成confirmed或替代核心多选；真实但未满足该门的辅助材料可保留为未验证背景，明确不参与完成。
- [ ] 用同一批离线测试验证：漫游、仅一步、iOS16手势限制、跨节拼接、桌面事实回答手机问题、多个format冒充多张图片全部不能完成。

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
UV_OFFLINE=1 uv run pytest -q tests/test_phase8_research_tasks.py tests/test_phase8_research_contract.py tests/test_phase8_research_supervisor.py
```

预期：新增行为先红后绿，既有负例继续通过；无法解析时明确partial而非假成功。**但仅能拒绝不能算业务功能完成**，下一步必须验证存在答案的正例。

**批次出口：** 保存任务字段/原问题锚点/证据匹配谓词、正反例、兼容策略。任何schema/API/持久化变化由Codex先评审记录，再放行Worker。

## 5. Task C：正文结构、取证闭环与协议稳定性

- [ ] 先写公开夹具行为测试：Apple嵌套手机选项及Shared Library注释不丢；Dropbox含`"+"`按钮的完整步骤不能因展示容器产生假缺口；正文读取失败必须区分未取得资料与没有此能力。
- [ ] 删除只含`site:`的空任务查询；查询必须含非空目标实体/动作/缺口词。去重等价URL时保留实际来源和抓取定位，不合并两份不同内容；避免#7两URL导致重复14条。
- [ ] 把补搜与缺失要求关联，停止条件/跳过原因入trace。在原预算内优先补关键缺口，不能无条件重复三轮同片段，也不能每项需求各偷偷开三次模型调用。
- [ ] 保持上限：3逻辑搜索、5正文/5来源、每页解压后4MiB/24000字符/10秒/3跳、最多100个给模型的单元、最多40选择、总LLM3次。若Task B需要一个规划调用，整体可采用“规划一次→证据/分析候选一次→有界纠错一次”；规划+所有重试均计入3次总额。做不到时报告预算缺口，不暗改成本上限。
- [ ] 新建v6版本schema，简化summary约束并明确它的角色。内部上下文摘要不冒充研究答案；对过长/漏字段/错误工具块发出不同安全错误码。不要把`summary_invalid`统称来源不足。
- [ ] 针对安全metadata中的end_turn/max_tokens/缺tool_use分别回放；不盲目截断JSON、不解析thinking、不使用正则从任意模型文字挖一个“合法对象”。模型返回不合法就有界纠错并如实降级。
- [ ] #7引用表达的修复先评审新结构化引文契约：ID+offset/length→快照逐字提取→同等严格来源/名称/数量/引文外扩写检查。可将含双引号正文作为结构化quote值展示，不能修改旧v14字符串负例让不精确文本通过；不能删掉按钮那一步再称完整。新通道若不能证明等价严格性，仍标缺口。

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
UV_OFFLINE=1 uv run pytest -q tests/test_phase8_research_reader.py tests/test_phase8_reader_supervisor.py tests/test_phase8_research_metadata.py tests/test_phase8_research_pipeline.py tests/test_phase8_research.py tests/test_phase8_research_v4.py
UV_OFFLINE=1 uv run ruff check app tests alembic scripts
```

**批次出口：** 搜索/正文/协议三类失败可区分，原失败正例能恢复所需证据，负例没有变松；保存每次请求预算。不得只增加Prompt关键词。

## 6. Task D：恢复竞品分析、模式、机会与风险

### 事实与分析的边界

必须先审查现有`ResearchFinding`两态和source_refs不变量。不得把推断强行置confirmed，也不得给not_verified塞source_refs破坏旧不变量。建议新增的可空语义字段为`claim_kind`和`supporting_finding_ids`，具体DTO/存储类型由Codex在本任务的架构评审中锁定后再实现：

- `claim_kind=fact`：来自同Run精确证据，明确产品/平台/范围；须过严格校验。
- `claim_kind=inference/recommendation`：清楚标注推断/建议，指向已确认事实；不参与Run完成。仍保持旧status/source_refs语义，不能因“有依据”自动宣称“已证实”。
- 市场模式如果是概括推断，按推断展示；只有单产品资料时不得称普遍市场趋势。优点/缺点须注明相对哪种需求/用户场景，不能由模型自称客观优劣。
- 未知旧行字段NULL，旧页面/历史查询继续工作；持久化中真正的业务支持关系不能只藏在execution_trace后让UI猜测。若需要新迁移，新增0015并测试旧行，绝不重写0014。

- [ ] 新建`research_claims.py`和相应行为测试；先验证有事实的夹具能生成正确分类并保存支持关系，不能仅断言字段/数组存在。
- [ ] 加拒绝测试：未知事实ID、跨Run/项目、未验证事实作唯一依据、循环支持、无来源能力否定、单产品推全市场、建议冒充官方声明、任意修改PM决策均拒绝/降级。
- [ ] 在`research_selection.py`中移除“永远空分类”的能力封死；候选只有在通过相应校验才持久化，不为了演示强制每类非空。
- [ ] 通过`research_service.py`和API接线，不新增任意外部写能力；对真实资料不足输出类别空缺原因，与“实现不支持此类别”区分。
- [ ] 以真实竞品研究另建分析题集，包含中文与不同表达，以及足以支持模式/机会的资料；与原十题分别统计，不能拿更简单新题替换原题成绩。

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
UV_OFFLINE=1 uv run pytest -q tests/test_phase8_research_claims.py tests/test_phase8_research_pipeline.py tests/test_phase8_research.py
UV_OFFLINE=1 uv run ruff check app tests alembic scripts
```

**批次出口：** 公开夹具的能力与拒绝行为通过；真实业务输出仍须Task F逐条来源核对，自动测试不负责签发语义PASS。

## 7. Task E：前端与Research后核心链路

- [ ] 后端DTO稳定后续原前端线程，只放行typed API与Insight详情/测试；只有实际DTO需要才增加API client读映射，不改mock生成“漂亮答案”。
- [ ] 事实、推断、建议分开显示；用户能从分析追到支持事实，再到原始外部来源。没有实际内容的分类显示真实空缺原因，不能用占位营销文案替代。
- [ ] 保留完整性未验证、人审状态、逐项缺口、旧Run可空兼容、项目切换清理；将内部码译为中文提示，原文引用可以保留来源语言。
- [ ] 在全新隔离库，用现有真实核心链路产生可用Insight，浏览器完成Research→查看发现/来源→PM明确决策→创建Requirement→真实生成PRD。种子直接造approved Requirement只覆盖Tool验收，不能替代这条链。
- [ ] 不默认把Research自动塞入Internal Evidence或PRD；原需求的链路允许PM参考Research后做决策。若PRD显式引用外部研究，必须用独立来源类型/追踪关系，不能伪造feedback external_id。未经单独评审不修改Phase5/6核心业务。
- [ ] 审核PRD中的Evidence和业务承诺是否真实；模型失败/来源不足时原工作流仍可用。生成了一个文件或200响应不代表PRD质量通过。
- [ ] 继续执行模拟Prepare→Cancel→Prepare→Approve、重复Approve、跨项目404；新Prepare后合法approved→archived再Approve409，无新增目标，Raw全字段不变。

```bash
cd /Users/salmon/Desktop/InsightFlow/frontend
npm run lint
npm test -- --reporter=dot
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:18080 npm run build
```

端口只是历史推荐，先检查实际空闲/归属。最终构建改变后需复查真实页面，不能把旧构建截图当新代码证明。浏览器截图、网络/API/DB证据保存在本轮独立目录；结束关闭自己创建的服务。

## 8. Task F：最终工程、真实研究与交付Gate

- [ ] 最后后端改动后，在backend依次执行最终门槛并保存退出码；先读verify_postgres脚本确认仍使用全新临时库：

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
UV_OFFLINE=1 uv run pytest -q
UV_OFFLINE=1 uv run --with pgserver==0.1.4 python scripts/verify_postgres.py
UV_OFFLINE=1 uv run ruff check app tests alembic scripts
```

PG预期全量通过及完整upgrade/check→downgrade base→upgrade/check，检查`POSTGRES_VERIFICATION_PASSED`；如有0015纳入整链。SQLite跳过项单列，不能合并成通过。

- [ ] 最后前端改动后执行Task E三个命令；工程过后完成最终构建的真实浏览器、Research后核心链路、模拟审批和Raw审计。
- [ ] 从根目录检查配置并真实执行原十题；不输出密钥、不改原题集：

```bash
cd /Users/salmon/Desktop/InsightFlow
UV_OFFLINE=1 uv run --project backend --with pgserver==0.1.4 python backend/scripts/verify_phase8_research_live.py --check-config
UV_OFFLINE=1 uv run --project backend --with pgserver==0.1.4 python backend/scripts/verify_phase8_research_live.py --samples 10
```

`--check-config`只证明存在。UV_OFFLINE只影响依赖解析；模型/搜索仍联网。若确实缺配置才标NOT MEASURED，不根据历史cwd差异直接宣布缺密钥。

- [ ] 对照独立官方原文，分开统计完整/局部/未验证，事实误阳性、相关性误收、完整性误阳性；未验证摘要也检查是否误导。已明确有答案的#6/#7/#9不能因为程序不支持而当作“资料不足”；#2/#5无法证明明确多选时允许诚实缺口，不能以Run必须10/10 completed逼迫造假。
- [ ] 对新的中文/改写/分析集单列结果；真实证据充分时应展示实际分析输出，证据不足可不生成。完成数增加不等于功能/质量全部达标。
- [ ] 若声称“稳定”，至少在同一最终代码上再进行一轮同集真实检验，报告两轮差异、协议失败和预算。此为本计划建议的稳定性证据，不是原PRD已规定的百分比门槛；不拿最好一轮覆盖失败一轮。
- [ ] 更新工作记录、08/09、交付报告、人工复测流程和入口，归档脱敏证据与SHA256、Worker账本、最终代码指纹，核对所有保护文件。

### 最终判断规则

工程、Research业务功能、真实质量、项目隔离/模拟审批、真实核心链路和文档全部满足，才由Codex审查精确暂存范围后本地提交。禁止`git add .`；排除ZIP、key、缓存、临时DB和本机敏感日志。正式三文鱼验收继续由本人完成，Codex不能代签。用户正式签字与自动工程验收分开记录。

任何业务分类仍硬编码为空、复杂要求靠全面拒绝掩盖、已知答案未能完整取得、出现无依据确认结论或链路未测，保持PARTIAL，不提交。保留失败Run与原文、说明哪个门槛未过。MCP/真实外部集成仍可DEFERRED，SIM只能叫模拟。Phase2/3/4/6/7历史限制继续保留，不能因本轮通过补签。

Phase8完成或失败交付后均STOP，不进入Phase9。新对话的启动文本见[41号文件](../../41-Phase8业务补齐新对话启动Prompt.md)。

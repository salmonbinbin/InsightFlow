# InsightFlow Phase 7 新版原型审核修订复验报告

## 结论

**CONDITIONAL PASS（静态交互原型）**。本次审查对象是三文鱼重新提供的桌面端 SaaS 原型 ZIP，修订内容另存新包。**Phase 7 Frontend Development Readiness：NO**。本报告不代表后端 Phase 1～6 的语义质量、真实模型评测或人工验收通过，也不代表已经启动正式前端开发。

## 范围与页面

实际检查并在浏览器打开 11 页：Overview、Feedback、Upload、Analysis、Insights、Insight Detail、Requirement List、Requirement、PRD List、PRD、Evaluation。反馈详情通过 Drawer 呈现。

| 页面 / 流程 | 修订后结论 | 说明 |
| --- | --- | --- |
| Overview、Feedback、Insights | PARTIAL | 路径与主要数据展示可用；汇总仍为 Mock，个别次要控件仅视觉占位。 |
| Upload | PARTIAL | 真实选择并本地解析 CSV，明确不入库；正式导入仍待 API。 |
| Analysis | PARTIAL | 阶段和错误状态可展示；Run/Retry 仅模拟。 |
| Insight Detail、Evidence、PM Decision | PARTIAL | 证据 ID 可追到原型 Raw 样本，评分与决策门禁自洽；真实证据完整性待后端。 |
| Requirement List、Requirement | PARTIAL | 优先级继承 PM 最终决策；编辑只在浏览器会话保存。 |
| PRD List、PRD | PARTIAL | 版本查看、编辑、Accept 示例可用；生成和服务端持久化待对接。 |
| Evaluation | PARTIAL | Actual 与 Target 分离，实际值统一 NOT MEASURED；无真实 Eval 任务。 |

## 主要发现与修复

| 优先级 | 原问题 | 修订 |
| --- | --- | --- |
| P0 | Overview/Evaluation 页面脚本异常 | 修复后浏览器可渲染。 |
| P0 | 评分权重、总分、AI Priority 与 PM 决策/Requirement 相互冲突 | 将示例五维权重统一为 100%，按阈值计算 AI Priority，四条已有 Requirement 对应有效 PM 决策；INS-005 演示 Override。 |
| P0 | Evidence Drawer 伪造反馈 ID 与正文 | 仅列存在于 Mock Feedback 的记录；演示汇总与可核查样本分别标注。 |
| P0 | 上传任意 CSV 都显示固定记录，并暗示部分成功入库 | 改为实际文件解析与本地校验；错误整批失败；结果明确未入库。 |
| P1 | 决策 Override 跳两版、Defer/Reject 保留旧 PM Priority | 一次操作升一版，清空无效 PM Priority，决策在当前浏览器会话保存。 |
| P1 | Requirement/PRD Save 不保存正文；无效 ID 静默跳转 | 当前会话内持久保存；无效 ID 显示 Not Found。 |
| P1 | Evaluation 模拟生产指标与 100% 人工复核 | 指标改为 NOT MEASURED，禁用真实运行入口；文案明确演示。 |
| P1 | 390px 多页横向溢出、固定 1440 viewport | 使用设备宽度与响应式布局，表格局部滚动。 |
| P2 | 项目切换入口展示不存在的数据池 | 明示只提供当前演示项目。 |

## 核心链路与 API 边界

原型展示 `Feedback → Insight → Evidence → Opportunity Score → AI Priority → PM Decision → Requirement → PRD → Evaluation`，其中 Evidence 必须区分 Raw Feedback 与 AI 总结。当前修订包通过 Mock 数据演示这条路径。Upload、Run Analysis、Evaluation、PRD Regenerate、项目切换和会话内保存均**不等同**后端请求。

正式前端需要将 Overview 聚合、Feedback 列表/详情、Insight 列表/详情、Decision 写入、Requirement/PRD 版本读写分别映射到当前 FastAPI 能力；Evaluation 应确认实际 API、评测数据集和人工复核流程。原型不应要求为其假运行状态改写后端。完整 API 映射仍以项目现有接口文档和开发前复核为准。

## 复验记录

- JS 语法检查：11 个 HTML 内联脚本及 `assets/data.js`、`assets/app.js` 均通过。
- Playwright 浏览器打开 11/11 页；抽查 Overview、Insight Detail、Evaluation 无控制台错误。
- 390px 宽复验 11 页；修订后页面无整体横向溢出，宽表格在局部区域滚动。视觉抽查 Overview 布局、导航和提示可读。
- 上传真实选择 `qa.csv`，预览显示所选内容；本地校验通过后结果仍为 `Imported 0 / 未入库`。
- Requirement/PRD 修改后刷新仍在当前会话保留；Override v1→v2；4 个无效详情参数均显示 Not Found；Evidence Drawer 展示 8 条存在于样本中的 ID。

## 尚未关闭

正式开发前需以真实 API 核对字段、分页、权限、项目隔离、版本冲突、服务端保存及失败状态；需要真实数据集和人工复核支持 Evaluation 的 Actual。原型中 528 条反馈/23 个问题是 Mock 汇总，可逐条查看的样本仅 30 条，不能用来证明真实覆盖率或语义正确性。历史 Phase 2/3 语义人工复核与 Phase 6 评测限制维持原状态。

仍需补齐 Evaluation 的 Severity MAE 展示，以及 Analysis 中 Embedding 和 Opportunity Scoring 的完整阶段表达；当前页面不可作为这几项已覆盖的验收证据。

**下一步**：三文鱼先审阅修订包与本报告；正式 Phase 7 前端开发仍需单独确认范围与验收门槛。本轮在原型修订与复验后 STOP。

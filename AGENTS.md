# InsightFlow：Codex Supervisor → Claude Code Worker

无论用户使用什么语言，完整回答使用中文，称呼用户为三文鱼。

本次三文鱼已明确授权 Phase 3 开发监督、验收和本地 Git 提交。旧 Phase 2 STOP 不代表本次未授权；Phase 2 真实语义 Embedding 和质量限制仍未关闭，必须保留在报告，不得宣称历史验收 PASS。

先读 AGENT_HANDOFF.md、技术开发规范与Git规则.md、工作记录.md、docs/11-长期开发总纲与Agent交接规范.md、docs/18-Phase3监督实施与验收方案.md。

- Codex 负责范围、架构、验收、Git；Claude 只实现明确下发的文件。
- 单一工作树同时只允许一个写入者。Worker 工作期间 Codex 不改本工作树文件；Worker 完成后 Codex 再验收。
- Claude 禁止任何 Git 命令、commit/push、修改 .git、AGENTS.md、CLAUDE.md、权限设置或借脚本绕过限制。
- Claude 使用 MCP edit tier + 本地 wrapper 映射的 acceptEdits + simple；只运行任务指定的测试命令。
- 保护 Raw Feedback、历史迁移0001—0007、已有测试、Phase1/2 Prompt与业务逻辑。不引入Phase4评分/人工决策、PRD、前端或重型队列。
- 失败必须由 Codex 提供证据，续接原 Claude threadId 修复。每次 jobId 均保存。
- 验收后 Codex 提交，禁止push。交付Phase3后STOP，不推进Phase4。真实模型评测及用户至少10条洞察复核分别记录，不互相冒充。

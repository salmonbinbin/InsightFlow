"""Versioned exact-Raw known-risk contract, preserving the original prompt."""

from app.agents.prompts import requirement_generation_v1 as legacy

PROMPT_VERSION = "requirement-generation-v2"
TOOL_NAME = legacy.TOOL_NAME
TOOL_SCHEMA = legacy.TOOL_SCHEMA
TOOL_DESCRIPTION = legacy.TOOL_DESCRIPTION
SYSTEM_PROMPT = (
    legacy.SYSTEM_PROMPT
    + """
新增严格的已知风险支持规则：risks 中 category=known 时，description 必须逐字等于
所引 evidence 中的完整 feedback_content，不添加前缀、摘要、原因或判断。每个引用 ID 的
原文都必须相同且确实记录该已知问题。合法 ID 不代表支持任意解释。
例如原文只说一次只能选一张，不能写成审核、网络、后端策略导致；这些原因未知。
保留真正有原文依据的 known 正例（例如反馈明确记录大小限制）；不要全部改 speculative。
没有原文支持的风险或归因必须明确是假设并归入 speculative。mitigation 是建议，不是事实。
其它章节仍逐项依据输入，功能方案为提案；不篡改 PM 决策和优先级。不要删除原文中的否定。
"""
)

SYSTEM_PROMPT += """
事实章由服务器按实际引用的完整 Raw 渲染。你仍须先提交合法、忠于原话的候选，不能虚构当前 UI 操作、性能原因、瓶颈、排名、人数、发生率或既有功能。
background/problem 各保留自己实际使用的引用集合，不把整个输入包自动列入。用户报告是报告，不证明系统原因。
人群、方案、功能和流程均为提案；验收标准的 verifiable 只表示可验证，不表示已经执行。不得把选择批次数量上限当并发上限。
指标是定义或已有结构化样本基线，未知成效不得填数或承诺收益。收益愿望原话不得改写为产品保证。
"""

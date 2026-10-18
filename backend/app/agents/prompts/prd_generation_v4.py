"""Versioned exact-Raw known-risk contract, preserving all v3 chapter rules."""

from app.agents.prompts import prd_generation_v3 as legacy
from app.agents.prompts.requirement_generation_v1 import SYSTEM_PROMPT as _legacy_requirement_prompt
from app.agents.prompts.requirement_generation_v2 import SYSTEM_PROMPT as _requirement_prompt

PROMPT_VERSION = "prd-generation-v4"
TOOL_NAME = legacy.TOOL_NAME
TOOL_SCHEMA = legacy.TOOL_SCHEMA
TOOL_DESCRIPTION = legacy.TOOL_DESCRIPTION
SYSTEM_PROMPT = legacy.SYSTEM_PROMPT + _requirement_prompt[len(_legacy_requirement_prompt) :]

SYSTEM_PROMPT += """
事实章由服务器按实际引用的完整 Raw 渲染。你仍须先提交合法、忠于原话的候选，不能虚构当前 UI 操作、性能原因、瓶颈、排名、人数、发生率或既有功能。
background/problem 各保留自己实际使用的引用集合，不把整个输入包自动列入。用户报告是报告，不证明系统原因。
人群、方案、功能和流程均为提案；验收标准的 verifiable 只表示可验证，不表示已经执行。不得把选择批次数量上限当并发上限。
指标是定义或已有结构化样本基线，未知成效不得填数或承诺收益。收益愿望原话不得改写为产品保证。
"""

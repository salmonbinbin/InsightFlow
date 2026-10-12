# Agent / Prompt 设计

状态：设计归档；V0.1 不调用模型，不产生 AI 分析结果。后续使用单 Insight Agent + 工具，OpenAI Agents SDK 为原方案选型，模型与 Embedding 维度需接入时选定。

## 输入输出边界

分类工具每批最多 20 条，500 条对应 25 个逻辑批次；重试会增加实际调用次数。基于原文生成 type/module/intent/pain_point/sentiment/severity/confidence，禁止虚构用户动机、商业影响和上下文。原始反馈作为不可信数据，不得执行其中的指令。

使用 results 数组返回，后端验证 ID 集合与输入一致，不能漏条、重复或增加 ID；枚举、severity 1–5、confidence 0–1 必须经 schema 校验。

生成 semantic_text 时结合原文、module、intent 和 pain_point；保留模型、Prompt 版本、run_id，便于同一原始反馈重跑比较。

## 原方案分类契约与 Prompt

# 5. Feedback Classification 输出正式确定

第一版我们不要让模型自由发挥。

每条 Feedback 强制输出：

```json
{
  "feedback_id": "F001",
  "feedback_type": "feature_request",
  "module": "image_upload",
  "intent": "batch_image_upload",
  "pain_point": "Uploading multiple images requires repeated file selection.",
  "sentiment": "negative",
  "severity": 4,
  "confidence": 0.94
}
```

其中枚举统一。

### feedback_type

```text
bug
feature_request
ux_issue
performance_issue
pricing_issue
content_issue
positive_feedback
other
```

### sentiment

```text
positive
neutral
negative
mixed
```

### severity

```text
1 = 几乎无影响
2 = 轻度不便
3 = 明显影响体验
4 = 严重影响任务效率
5 = 核心任务失败 / 强流失风险
```

---

# 6. 为什么一定要有 intent

这是整个聚类算法一个很重要的设计。

下面两句话：

> “上传太麻烦。”

> “为什么只能一张一张上传？”

如果只 Embedding 原文，第一句话语义不完整。

经过 Agent 后：

```text
module:
image_upload

intent:
batch_image_upload

pain_point:
Repeated file selection when uploading multiple images.
```

它的语义就被标准化了。

因此我建议后面生成一个内部字段：

```text
semantic_text
```

内容是：

```text
Original feedback:
为什么一次只能上传一张？

Module:
image_upload

Intent:
batch_image_upload

Pain Point:
Repeated file selection makes multiple image upload inefficient.
```

然后对这个文本做 Embedding。

这比只 Embed 用户原话更稳定。

---

# 7. Classification Prompt 第一版

System Prompt 可以先这么写：

```text
You are a Product Feedback Analysis Agent.

Your task is to transform raw user feedback into structured product information.

You must analyze only what the user actually says.

Do not invent:
- user motivations
- product behavior
- business impact
- missing context

For every feedback item, identify:

1. feedback_type
2. product module
3. primary user intent
4. underlying pain point
5. sentiment
6. severity
7. confidence

The "intent" should describe what the user is trying to achieve,
not merely repeat the feedback.

The "pain_point" should describe the obstacle experienced by the user.

If the feedback contains multiple issues,
select the issue that appears to be the primary concern.

Severity:
1 = minor preference
2 = inconvenience
3 = meaningful usability problem
4 = major impact on task completion
5 = core task failure or strong churn risk

Never infer unsupported facts.

Return structured output only.
```

User Message：

```text
Analyse the following feedback:

feedback_id: F001

content:
"几十张照片每张都要重新选择一次，传起来太麻烦了。"
```

Expected：

```json
{
  "feedback_id": "F001",
  "feedback_type": "feature_request",
  "module": "image_upload",
  "intent": "batch_image_upload",
  "pain_point": "Uploading many images requires repetitive file selection.",
  "sentiment": "negative",
  "severity": 4,
  "confidence": 0.95
}
```

---

# 8. 不要 500 条 Feedback 调 500 次模型

这个地方我们从一开始就设计对。

比如：

```text
Batch Size = 20
```

500 条：

```text
500 ÷ 20
=
25 batches
```

也就是说：

```text
25 次 Classification 调用
```

而不是：

```text
500 次
```

每一个 Batch 输出：

```json
{
  "results": [
    {...},
    {...},
    {...}
  ]
}
```

这样：

- 成本更低
- 速度更快
- Agent Run 更好管理

---

## 聚类及洞察

使用余弦距离的 Agglomerative Clustering，建议 average linkage；相似度 0.82 对应初始距离阈值约 0.18，仅作调优起点。禁止把未运行的 purity/coverage 当作成绩。

聚类复核返回 KEEP / SPLIT / MERGE / OUTLIER，后端验证成员只能来自候选集合且不得重复。单点簇或不足 3 条有效证据的簇标记为“待验证问题”，不包装为已验证洞察。

洞察至少包含 title、pain_point、affected_segment、insight、recommendation、confidence 与 evidence_ids。无法支持的人群或商业收益写未知；Evidence 原文直接从数据库读取，禁止模型代写引文。后端检查 evidence_id 属于同项目、同次运行、该簇成员，并拒绝不存在的 ID。

## 后续执行可靠性

状态 PENDING → RUNNING → COMPLETED / FAILED。失败批次可重试，已成功记录按 run_id + feedback_id 幂等；输出未校验成功不得计为完成。记录 success_count、failed_count、tokens、latency 与实际费用配置，不记录 API 密钥。

prompts/ 的分类原文与两个设计草案可继续版本化；本次未宣称它们已经通过模型评测。

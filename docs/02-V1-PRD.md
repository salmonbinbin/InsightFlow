# 阅读说明

以下保留原对话完整基准 PRD。实现时以 03 文档的后续 Sprint 1 修订为准，特别是 Raw/Analysis 分离、intent 命名及 4 页面范围；当前 V0.1 状态见 README。所有示例数字不是项目成绩。

# InsightFlow V1 产品需求文档

**产品名称：** InsightFlow  
**产品定位：** AI Product Insight Agent  
**版本：** V1.0 MVP  
**核心目标：** 将大量非结构化用户反馈自动转化为可追溯、可排序、可执行的产品需求洞察。

---

# 1. 产品背景

产品经理在日常工作中会接触大量零散信息：

- 用户反馈
- 客服记录
- App 评论
- 用户访谈
- 社群反馈
- 问卷开放题
- 销售反馈

传统处理方式通常依赖人工阅读、Excel 分类和主观判断，存在几个明显问题：

1. 信息量过大，人工分析效率低。
2. 相同需求表达方式不同，容易重复统计。
3. 产品洞察容易依赖个人经验。
4. 很难证明某个产品判断到底来源于哪些用户反馈。
5. 需求优先级缺乏统一的数据依据。
6. 从“发现问题”到“形成 PRD”存在大量重复工作。

InsightFlow 希望通过 AI Agent 将这一过程标准化。

---

# 2. 产品目标

InsightFlow V1 要解决三个核心问题。

## 2.1 帮助 PM 看懂大量用户反馈

系统自动完成：

用户反馈 → 分类 → 意图提取 → 痛点识别 → 情绪判断

---

## 2.2 找到真正值得关注的问题

系统自动完成：

相似反馈 → 聚类 → 去重 → 用户痛点 → Product Insight

---

## 2.3 将洞察转化为产品行动

系统进一步完成：

Insight → Evidence → Priority → Requirement → PRD

最终形成：

**Voice of Customer → Product Decision**

---

# 3. V1 暂时不做什么

为了控制 MVP 范围，第一版暂不开发：

- 自动抓取小红书
- 自动抓取 App Store
- Jira 集成
- 飞书集成
- Notion 集成
- 企业微信集成
- 自动修改线上产品
- 自动创建开发任务
- 完整 Multi-Agent 系统
- 实时数据流分析
- 企业级权限系统

V1 的核心输入方式：

**CSV / Excel 文件上传**

这样可以把精力放在真正有价值的 Agent Pipeline 上。

---

# 4. 目标用户

## Primary Persona

### AI / Internet Product Manager

典型场景：

每周收到数百甚至数千条用户反馈，需要快速判断：

- 用户最不满意什么？
- 最近什么问题增长最快？
- 哪些反馈其实属于同一个需求？
- 哪个需求值得优先开发？
- 为什么要做这个功能？

---

# 5. 核心用户故事

### Story 1

作为产品经理，

我希望上传用户反馈文件，

从而快速了解近期用户主要讨论的问题。

---

### Story 2

作为产品经理，

我希望 AI 自动识别相似反馈，

避免将相同需求重复统计。

---

### Story 3

作为产品经理，

我希望所有 AI 洞察都有原始证据，

从而判断 AI 的结论是否可靠。

---

### Story 4

作为产品经理，

我希望系统自动评估需求优先级，

帮助我确定下一版本应该重点解决什么问题。

---

### Story 5

作为产品经理，

我希望在 AI 建议基础上进行人工调整，

而不是让 AI 完全替代我的决策。

---

### Story 6

作为产品经理，

我希望将确认后的 Insight 快速转成产品需求方案，

降低重复编写 PRD 的工作量。

---

# 6. 产品核心流程

```text
创建 Project

↓

上传 CSV / Excel

↓

数据解析

↓

Feedback Classification

↓

Intent Extraction

↓

Embedding

↓

Semantic Clustering

↓

LLM Cluster Refinement

↓

Insight Generation

↓

Evidence Binding

↓

Opportunity Score

↓

PM Review

↓

Create Requirement

↓

Generate PRD
```

---

# 7. 信息架构

左侧导航设计：

```text
InsightFlow

Overview

Feedback

Insights

Requirements

PRD

Evaluation

Settings
```

V1 重点完成：

- Overview
- Feedback
- Insights
- Requirements
- PRD
- Evaluation

---

# 8. 页面一：Project

用户进入系统后创建分析项目。

例如：

```text
Project Name

ChatGPT User Feedback Analysis


Product

AI Assistant


Description

Analyse recent user feedback and discover
high-value product opportunities.
```

一个 Project 对应一组产品反馈数据。

---

# 9. 页面二：Upload Feedback

支持：

- CSV
- XLSX

V1 标准字段：

| 字段 | 必填 |
|---|---|
| feedback_id | 是 |
| content | 是 |
| source | 否 |
| user_type | 否 |
| created_at | 否 |
| user_id | 否 |

系统允许用户进行字段映射。

例如：

```text
你的字段          InsightFlow字段

评论内容     →    content

用户类型     →    user_type

评论时间     →    created_at

渠道         →    source
```

点击：

**Start Analysis**

开始 AI Pipeline。

---

# 10. Feedback Intelligence

每条反馈经过 AI 后生成结构化结果。

示例反馈：

> 每次传照片只能一张一张选，真的很浪费时间。

输出：

```json
{
  "feedback_type": "feature_request",
  "module": "image_upload",
  "user_intent": "batch_image_upload",
  "pain_point": "single image upload is inefficient",
  "sentiment": "negative",
  "severity": 4,
  "confidence": 0.94
}
```

---

# 11. Feedback Type

第一版统一使用以下分类。

```text
Bug

Feature Request

UX Issue

Performance Issue

Pricing Issue

Content Issue

Positive Feedback

Other
```

---

# 12. Severity

使用 1～5 分。

### Severity 1

轻微意见，不影响正常使用。

### Severity 2

存在不便，但用户仍能完成任务。

### Severity 3

明显影响使用体验。

### Severity 4

严重影响核心任务完成。

### Severity 5

导致核心功能无法使用或用户流失风险明显。

---

# 13. Semantic Clustering

这是 V1 最重要的 AI 能力之一。

系统需要识别：

```text
一次只能传一张图片。

希望支持图片多选。

几十张照片一张一张上传太麻烦。

为什么不能批量上传？

上传很多照片效率很低。
```

实际上属于：

```text
Cluster

Batch Image Upload
```

---

# 14. 聚类技术方案

采用：

```text
Feedback

↓

Embedding

↓

Vector Similarity

↓

Initial Cluster

↓

LLM Semantic Review

↓

Final Cluster
```

不要直接把全部反馈丢给 LLM。

原因：

- 成本高
- 上下文有限
- 稳定性差
- 数据规模扩大后不可持续

Embedding 负责召回。

LLM 负责判断。

---

# 15. Cluster 数据

每个 Cluster 保存：

```text
cluster_id

cluster_name

summary

feedback_count

unique_user_count

average_severity

negative_ratio

growth_rate

confidence
```

---

# 16. Insight Generation

Cluster 不等于 Insight。

Cluster：

> 97 条用户反馈都在讨论批量上传。

Insight：

> 高频内容创作者在处理大量图片时，需要反复执行单图选择操作，导致内容发布效率下降。

AI 必须完成从：

**What users say**

到：

**What the real problem is**

的转化。

---

# 17. Insight Card

每个 Insight 在页面中显示：

```text
Batch Upload Efficiency

P0

97 Feedback

71 Users

↑ 21% vs Previous Period


Pain Point

High-frequency content creators need to
repeatedly select files when uploading
multiple images.


Affected Segment

Content Creators


Average Severity

4.3 / 5


Negative Sentiment

82%


Opportunity Score

8.6 / 10
```

---

# 18. Evidence System

这是 InsightFlow 最核心的产品设计之一。

所有 Insight 必须绑定原始 Evidence。

点击：

**View Evidence**

展示：

```text
User Feedback

“为什么不能一次选择多张照片？”

“几十张图一个个上传很麻烦。”

“希望支持图片多选。”

“一次只能传一张真的很浪费时间。”
```

每条证据必须可以追溯到：

```text
Feedback ID

Source

User

Date
```

设计原则：

> No Evidence, No Insight.

---

# 19. Evidence Grounding

AI 生成 Insight 时，只允许基于该 Cluster 中实际存在的反馈。

系统保存：

```text
Insight

↓

Evidence IDs

↓

Original Feedback
```

这样可以降低：

- Hallucination
- 无依据推断
- AI 过度概括

---

# 20. Opportunity Score

系统不会直接让 LLM 决定优先级。

采用结构化评分模型。

第一版：

```text
Opportunity Score

=

User Coverage × 25%

+

Severity × 25%

+

Growth Trend × 15%

+

User Value × 20%

+

Business Value × 15%
```

最终转换成：

```text
0 - 4.9

P2


5.0 - 7.4

P1


7.5 - 10

P0
```

---

# 21. AI 与规则如何分工

AI 负责：

- Severity 判断
- User Value 判断
- Intent 理解
- Pain Point 理解

系统算法负责：

- Feedback Count
- User Count
- Growth Trend
- Opportunity Score

PM 负责：

- Business Value
- 最终 Priority
- 最终是否进入需求池

这一设计非常关键。

InsightFlow 的产品哲学是：

**AI recommends, PM decides.**

---

# 22. Human-in-the-loop

Insight 页面提供：

```text
AI Recommendation

Priority: P1

Opportunity Score: 7.2
```

产品经理可以点击：

**Override Priority**

修改为：

```text
P0
```

系统要求填写理由：

```text
Why?

○ Strategic Priority

○ Core User Experience

○ Revenue Impact

○ Development Cost

○ Management Decision

○ Other
```

例如：

```text
Search is the main product path.

Although the feedback volume is lower,
it directly affects retention.
```

系统保存：

```text
AI Priority

P1


PM Priority

P0


Override Reason

Core User Experience
```

---

# 23. Requirement

产品经理点击：

**Create Requirement**

系统根据 Insight 创建需求。

结构：

```text
Requirement Name

Problem

Target User

User Story

Solution Hypothesis

Priority

Evidence

Success Metrics

Risks
```

---

# 24. PRD Generator

点击：

**Generate PRD**

AI 根据：

- Insight
- Evidence
- PM Decision
- Requirement

生成完整 PRD 草稿。

PRD 内容：

```text
1 Background

2 Problem

3 User Persona

4 User Story

5 Product Goal

6 Solution

7 Functional Requirements

8 User Flow

9 Edge Cases

10 Acceptance Criteria

11 Metrics

12 Risks

13 MVP Scope
```

---

# 25. PRD 必须支持人工编辑

AI 输出只是 Draft。

页面上必须明确：

```text
AI Generated Draft
```

用户可以：

- 编辑
- 删除
- 重写某段
- Regenerate
- Accept

避免设计成：

**AI 自动决定产品方案。**

---

# 26. Overview Dashboard

Overview 首页显示：

```text
Total Feedback

528


Detected Issues

23


High Priority Insights

5


Negative Feedback

63%
```

下面：

## Top Insights

```text
Search Accuracy

128 feedback

Score 9.1

P0
```

```text
Batch Upload

97 feedback

Score 8.6

P0
```

```text
Response Speed

72 feedback

Score 7.7

P0
```

---

# 27. Trend

如果数据包含时间字段，显示：

```text
Feedback Trend
```

例如：

```text
Search Accuracy

Week 1      12

Week 2      18

Week 3      29

Week 4      46
```

系统给出：

```text
↑ 155%

Rapidly Increasing
```

这种趋势非常适合产品经理判断：

“最近突然发生了什么？”

---

# 28. Agent 架构

V1 不做 Multi-Agent。

采用：

```text
Insight Agent

│

├── File Parser Tool

├── Feedback Classification Tool

├── Embedding Tool

├── Cluster Tool

├── Database Tool

├── Insight Generator Tool

├── Opportunity Score Tool

└── PRD Generator Tool
```

即：

**Single Agent + Multiple Tools**

---

# 29. Agent System Prompt 核心原则

Agent 身份：

```text
You are an AI Product Insight Analyst.

Your role is to help product managers
identify user problems and product
opportunities from user feedback.

You must not invent user needs.

Every important conclusion must be
supported by evidence from the provided
feedback dataset.

You provide recommendations,
but final product decisions belong to
the product manager.
```

---

# 30. Agent 工作流

```text
AnalyseFeedback

↓

ClassifyFeedback

↓

ExtractIntent

↓

GenerateEmbedding

↓

ClusterFeedback

↓

ReviewCluster

↓

GenerateInsight

↓

AttachEvidence

↓

CalculateOpportunityScore

↓

HumanReview

↓

GenerateRequirement

↓

GeneratePRD
```

---

# 31. 数据库设计

## projects

```text
id

name

product_name

description

created_at
```

---

## feedbacks

```text
id

project_id

content

source

user_id

user_type

created_at

feedback_type

module

intent

pain_point

sentiment

severity

confidence

embedding
```

---

## clusters

```text
id

project_id

name

summary

feedback_count

unique_user_count

avg_severity

negative_ratio

growth_rate
```

---

## cluster_feedback

```text
cluster_id

feedback_id
```

---

## insights

```text
id

project_id

cluster_id

title

pain_point

affected_segment

recommendation

opportunity_score

ai_priority

pm_priority

status
```

---

## evidences

```text
id

insight_id

feedback_id

relevance_score
```

---

## requirements

```text
id

project_id

insight_id

name

problem

target_user

user_story

solution

priority

status
```

---

## prds

```text
id

requirement_id

content

version

status
```

---

## agent_runs

```text
id

project_id

workflow

model

input_tokens

output_tokens

latency

status

created_at
```

---

## evaluations

```text
id

agent_run_id

metric

score

expected

actual
```

---

# 32. Evaluation 页面

这是简历展示重点。

展示：

```text
Feedback Classification Accuracy

92%


Intent Accuracy

90%


Cluster Purity

87%


Evidence Accuracy

96%


Tool Success Rate

98%


Hallucination Rate

3.4%
```

---

# 33. Golden Dataset

准备：

**100 条人工标注反馈。**

人工提前标注：

```text
Feedback

一次只能传一张图，太麻烦了。


Ground Truth

Type:
Feature Request

Module:
Upload

Intent:
Batch Upload

Severity:
4
```

Agent 跑完后与 Ground Truth 比较。

---

# 34. Evaluation Metrics

V1 优先实现：

### Classification Accuracy

AI 是否正确识别 Feedback Type。

### Intent Accuracy

是否识别正确用户真正想做什么。

### Cluster Purity

同一个 Cluster 是否真的属于同一个需求。

### Evidence Accuracy

Insight 引用的 Evidence 是否真的支持结论。

### Hallucination Rate

Insight 是否出现数据中没有依据的判断。

### Agent Success Rate

整个工作流完成率。

---

# 35. 技术架构

```text
Frontend

Next.js

React

Tailwind CSS

shadcn/ui

ECharts


↓

API


FastAPI


↓

Agent Layer


OpenAI Agents SDK


↓

AI

LLM

Embedding


↓

Data

PostgreSQL

pgvector
```

---

# 36. 前端页面

第一期开发顺序：

### P0

Login 可以先不做。

直接进入 Demo Project。

---

### Page 1

Project Dashboard

---

### Page 2

Upload Feedback

---

### Page 3

Feedback

---

### Page 4

Insights

---

### Page 5

Insight Detail

---

### Page 6

Requirement

---

### Page 7

PRD

---

### Page 8

Evaluation

---

# 37. MVP 验收标准

V1 完成必须满足：

用户可以上传：

> 300 条以上 CSV 用户反馈。

系统可以自动：

> 分类用户反馈。

系统可以自动：

> 识别相似需求。

系统可以自动：

> 形成 Insight。

每个 Insight：

> 至少拥有 3 条原始 Evidence。

系统可以：

> 自动计算 Opportunity Score。

用户可以：

> 修改 AI Priority。

系统可以：

> 将 Insight 转为 Requirement。

系统可以：

> 自动生成 PRD Draft。

系统可以：

> 展示基础 Agent Evaluation 数据。

满足以上条件即可定义：

**InsightFlow V1 MVP Completed**

---

# 38. Demo Dataset

第一版建议模拟一个：

**AI Chat Product**

准备约：

**500 条用户反馈**

主要包含：

```text
Search Accuracy

Response Speed

Memory

File Upload

Image Upload

Conversation Management

Voice

Mobile App

Subscription

UI

Model Quality
```

故意设计：

约 20～30 个真实需求 Cluster。

这样 Dashboard 会非常漂亮。

---

# 39. Demo 演示流程

面试时控制在 2～3 分钟。

### Step 1

上传：

```text
500 User Feedback
```

---

### Step 2

点击：

**Analyze**

---

### Step 3

Dashboard 出现：

```text
23 Issues Detected

5 High Priority
```

---

### Step 4

点击：

**Batch Image Upload**

显示：

```text
97 Feedback

71 Users

82% Negative

Opportunity Score 8.6
```

---

### Step 5

点击：

**Evidence**

展示真实用户反馈。

---

### Step 6

AI：

```text
Priority P1
```

PM：

```text
Override → P0
```

理由：

```text
Core creator workflow.
```

---

### Step 7

点击：

**Generate Requirement**

---

### Step 8

点击：

**Generate PRD**

生成：

```text
User Story

MVP Scope

Acceptance Criteria

Metrics
```

---

### Step 9

打开 Evaluation。

展示：

```text
Classification 92%

Evidence Accuracy 96%

Hallucination 3.4%
```

Demo 结束。

整个故事形成完整闭环：

```text
User Voice

↓

AI Insight

↓

Evidence

↓

Human Decision

↓

Product Requirement

↓

PRD

↓

Evaluation
```

---

# 40. 开发阶段

## Sprint 1

目标：

**Feedback → Insight**

开发：

- Project
- CSV Upload
- Feedback Parser
- Feedback Classification
- Embedding
- Clustering
- Insight Generation

完成后系统已经可以：

> 上传反馈 → 得到洞察

---

## Sprint 2

目标：

**Insight → Product Decision**

开发：

- Insight Detail
- Evidence
- Opportunity Score
- Priority
- Human Override

---

## Sprint 3

目标：

**Product Decision → PRD**

开发：

- Requirement
- PRD Generator
- PRD Editor

---

## Sprint 4

目标：

**AI Evaluation**

开发：

- Golden Dataset
- Eval Script
- Evaluation Dashboard
- Agent Runs
- Cost / Latency Tracking

---

# 41. V2 扩展方向

V1 做完以后再增加：

### Research Agent

自动研究竞品。

### Web Search

获取外部产品信息。

### MCP

连接第三方产品工具。

### Jira

自动创建 Feature / Story。

### Notion

同步 PRD。

### Feishu

同步需求文档。

### Customer Support

自动同步客服反馈。

### Multi-Agent

```text
Product Manager Agent

↓

Insight Agent

Research Agent

PRD Agent
```

---

# 42. 项目最终价值

这个项目不应该被描述为：

> 使用大模型分析用户反馈。

而应该描述为：

> 构建面向产品经理的 AI Product Insight Agent，通过用户反馈结构化、语义聚类、Evidence Grounding、需求机会评分和 Human-in-the-loop 决策机制，将 Voice of Customer 转化为可执行产品需求，并建立 Agent Evaluation 体系评估其可靠性。

这才是 InsightFlow 最终应该呈现的产品价值。
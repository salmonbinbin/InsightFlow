# 阅读说明

以下保留完整 Sprint 1 原方案。主题合计为 492 而非 500；修正见 07。所有百分比均为目标或示意。最后的 V0.1 建议已由本次开发承接。

好。我们现在正式进入 **InsightFlow Sprint 1：Feedback → Insight**。这一阶段不要碰 PRD Generator、MCP、多 Agent，目标只有一个：

> **上传 500 条用户反馈 → AI 自动理解 → 相似问题聚类 → 输出有 Evidence 支撑的 Product Insight。**

只要这条链真正跑通，InsightFlow 就已经有第一个可 Demo 的核心能力。

---

# 1. Sprint 1 最终效果

这阶段做完后，用户应该能完成：

```text
创建 Project
    ↓
上传 500 条 CSV / Excel 用户反馈
    ↓
系统解析数据
    ↓
AI 批量分类
    ↓
Embedding
    ↓
语义聚类
    ↓
Cluster Refinement
    ↓
生成 Product Insight
    ↓
查看原始 Evidence
```

例如输入：

```text
“一次只能传一张照片，好麻烦。”

“能不能一次选十几张图片？”

“我每次发几十张图都要点半天。”

“希望支持图片多选。”
```

最后系统不是显示四个问题，而是：

```text
Batch Image Upload
批量图片上传效率问题

28 Feedback
21 Users
Average Severity: 3.9
83% Negative

Insight:
高频图片用户在上传多张图片时需要反复执行文件选择操作，
增加了内容发布过程中的操作成本。

Evidence:
#F102 “一次只能传一张照片，好麻烦。”
#F183 “希望支持图片多选。”
#F239 “我每次发几十张图都要点半天。”
```

这就是 Sprint 1 的验收结果。

---

# 2. Demo 数据集怎么设计

不要直接随机生成 500 条完全无规律的句子。

我们人为埋入 **20 个真实需求主题**，这样后面才能判断 Agent 的聚类到底准不准。

我建议把产品背景定义成：

> **某 AI Chat Assistant 的用户反馈**

500 条数据分布如下：

| Cluster | 数量 | 典型问题 |
|---|---:|---|
| Response Quality | 55 | 回答错误、答非所问 |
| Search Accuracy | 42 | 搜索结果不相关 |
| Response Speed | 35 | 回答太慢 |
| Memory | 35 | 记不住之前的信息 |
| Subscription & Pricing | 32 | 价格、订阅权益 |
| File Upload | 30 | PDF/文件上传问题 |
| Context Loss | 30 | 长对话丢失上下文 |
| Image Upload | 28 | 多图片上传、识图 |
| Mobile Experience | 28 | 手机端体验 |
| Conversation Management | 24 | 分类、搜索、删除聊天 |
| Voice | 22 | 语音识别/Voice体验 |
| Model Availability | 20 | 模型限制、切换 |
| UI / Interaction | 20 | UI、操作流程 |
| Multilingual | 18 | 中文/多语言表现 |
| Web Browsing | 18 | 联网结果质量 |
| Export / Sharing | 18 | 导出、分享聊天 |
| Login / Account | 14 | 登录和账号问题 |
| Privacy | 12 | 数据隐私 |
| Positive Feedback | 8 | 正向反馈 |
| Other | 3 | 无法归类 |
| **总计** | **500** | |

不过这里有一个重要设计。

## 数据集实际上保留两层

**用户上传时看到的是 Raw Dataset：**

```csv
feedback_id,content,source,user_id,user_type,created_at
F001,"为什么每次只能上传一张图片？",support,U001,pro,2026-09-03
```

我们自己额外保存一个 **Ground Truth Dataset**：

```csv
feedback_id,
gt_feedback_type,
gt_module,
gt_intent,
gt_cluster,
gt_severity
```

例如：

```text
F001
Feature Request
Image Upload
batch_image_upload
image_upload
3
```

这一步非常关键。

因为后面做 Evaluation 时，我们可以真正计算：

```text
Prediction
vs
Ground Truth
```

而不是自己觉得“AI 好像分析得不错”。

---

# 3. 500 条数据不要全都写得一样

为了让项目真实，必须故意制造难度。

例如 Batch Upload 这个 Cluster 不能全是：

> 希望批量上传图片。

还应该出现：

```text
一次只能选一张真的烦。

我传旅游照片的时候要点几十次。

为什么不能像微信一样多选？

大量图片操作效率太低了。

上传这里能不能加个全选？

做内容的人一次几十张图，这个流程太痛苦了。
```

甚至可以加入：

```text
上传图片其实挺快的，就是每次只能选一张。
```

它：

- sentiment 可能不是纯 Negative
- 但是 intent 依然是 Batch Upload

这样才能测试 Agent 的**语义理解能力**。

---

# 4. 数据源也要模拟真实

不要让全部数据都是 `App Store`。

建议分成：

```text
In-App Feedback       35%
Customer Support      25%
App Store             15%
Community             15%
User Interview        10%
```

用户类型：

```text
Free       55%
Pro        35%
Team       10%
```

这样未来 Dashboard 可以进一步发现：

> “虽然批量上传只占全部反馈的 5.6%，但其中 67% 来自 Pro 用户。”

这就开始有真正的产品洞察价值了。

---

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

# 9. 数据库结构这里我要修改我们之前的一点设计

之前我们把：

```text
feedback_type
intent
severity
```

直接存在 `feedbacks`。

真正开发时我建议**不要这样做**。

Raw Data 和 AI Analysis 应该分离。

因为以后 Prompt V1 和 Prompt V2 的结果可能不同。

更合理：

```text
feedbacks
│
├── 原始用户数据
│
└── 永远不修改

feedback_analyses
│
├── AI输出
├── model
├── prompt_version
├── run_id
└── confidence
```

这样以后可以说：

> “我们升级 Prompt 后重新跑了一轮 Analysis，并比较两个版本的 Evaluation。”

这个设计非常专业。

---

# 10. Sprint 1 数据库最终版本

### projects

```text
id
name
product_name
description
created_at
```

### feedbacks

```text
id

project_id

external_id

content

source

user_id

user_type

feedback_created_at

created_at
```

### analysis_runs

```text
id

project_id

run_type

model

prompt_version

status

input_count

success_count

failed_count

input_tokens

output_tokens

latency_ms

created_at
```

### feedback_analyses

```text
id

feedback_id

run_id

feedback_type

module

intent

pain_point

sentiment

severity

confidence

semantic_text

created_at
```

### feedback_embeddings

```text
id

feedback_analysis_id

embedding_model

embedding

created_at
```

### clusters

```text
id

project_id

run_id

name

summary

feedback_count

unique_user_count

average_severity

negative_ratio

confidence
```

### cluster_members

```text
cluster_id

feedback_analysis_id

similarity_score
```

### insights

```text
id

cluster_id

title

pain_point

affected_segment

insight

recommendation

confidence

created_at
```

### insight_evidence

```text
insight_id

feedback_id

relevance_score
```

这就是 Sprint 1 的核心数据库。

---

# 11. 然后进入最关键的 Clustering

V1 不要搞非常复杂的 Machine Learning。

500 条数据这个规模：

### Step A：Embedding

得到：

```text
F001 → vector
F002 → vector
F003 → vector
...
```

---

### Step B：Cosine Similarity

计算：

```text
similarity(F001,F002)
```

越接近：

```text
1
```

语义越相似。

例如：

```text
“一次只能传一张。”

“希望支持多图选择。”
```

可能非常接近。

---

### Step C：Agglomerative Clustering

我更推荐 Sprint 1 先用：

**Hierarchical / Agglomerative Clustering**

而不是直接 K-Means。

原因很简单：

K-Means 要提前告诉算法：

```text
K = ?
```

真实产品里我们本来就不知道用户到底有多少类问题。

Agglomerative 可以通过：

```text
distance_threshold
```

自动决定形成多少 Cluster。

---

# 12. Threshold 不要拍脑袋定死

例如初始：

```text
Cosine Similarity ≈ 0.82
```

可以先尝试。

但不要在项目里说：

> “相似度超过0.82就一定是同一需求。”

正确说法应该是：

> 0.82 是 Initial Threshold，通过 Golden Dataset 对 Cluster Purity 与 Coverage 进行评估后调优。

比如：

```text
0.75

Cluster数量：12
Purity：71%
```

```text
0.82

Cluster数量：21
Purity：88%
```

```text
0.90

Cluster数量：47
Purity：95%
但过度拆分
```

最后发现：

```text
0.83
```

效果最好。

这就是一个非常好的面试故事。

---

# 13. 为什么 Clustering 后还需要 LLM

Embedding 会遇到这种问题：

> “上传 PDF 太慢。”

> “上传图片太慢。”

语义上都包含：

```text
upload
slow
```

向量可能比较接近。

但是实际上：

```text
File Upload Performance
```

和：

```text
Image Upload Performance
```

可能是两个需求。

所以流程不是：

```text
Embedding
↓
结束
```

而是：

```text
Embedding

↓

Algorithm Clustering

↓

LLM Semantic Review
```

LLM 对 Cluster 进行：

```text
KEEP

SPLIT

MERGE

OUTLIER
```

判断。

---

# 14. Cluster Review Prompt

例如给 LLM：

```text
Cluster Candidate #17

1. 上传PDF经常失败。
2. PDF超过几十MB就传不了。
3. 为什么文件上传这么慢？
4. 图片上传一次只能选一张。
5. 上传文档有时候会卡死。
```

Agent 应该判断：

```text
SPLIT
```

拆成：

```text
Cluster A
File Upload Reliability
```

和：

```text
Cluster B
Batch Image Upload
```

这一步就是：

> **LLM semantic refinement**

也是你这个 Agent 项目很值得讲的技术设计。

---

# 15. Cluster → Insight 不要直接等号

例如 Cluster：

```text
Topic:
Batch Image Upload

28 Feedback
```

这只是“发生了什么”。

我们需要 Agent 再生成：

### Pain Point

```text
Users uploading multiple images must repeatedly
perform the same file-selection operation.
```

### Insight

```text
High-frequency image users experience unnecessary
interaction cost when handling multi-image tasks.
```

### Product Opportunity

```text
Supporting multi-image selection could reduce
repetitive operations in the image upload workflow.
```

注意这里的措辞：

是：

> **could**

而不是：

> 一定能够提高留存率 30%。

因为数据没有告诉我们留存率。

---

# 16. Evidence 不能让 LLM 自己编

这是我们的铁律：

> **No Evidence, No Insight.**

Evidence ID 必须来自实际数据库：

```text
F037
F102
F183
```

系统后端检查：

```python
evidence_id in cluster_feedback_ids
```

如果：

```text
false
```

直接拒绝 Agent 输出。

这就是一个简单的 **Guardrail**。

---

# 17. Evidence 怎么挑

不要随机取三条。

我们选：

```text
Representative Evidence
+
High Severity Evidence
+
High Similarity Evidence
```

例如最终显示：

```text
Evidence 1

最能代表 Cluster 中心语义
```

```text
Evidence 2

Severity 最高
```

```text
Evidence 3

另一个表达方式明显不同，
但属于同一个需求
```

这样展示出来更有说服力。

---

# 18. API 第一版也直接定下来

前端 → 后端：

```text
POST
/projects
```

创建 Project。

```text
POST
/projects/{project_id}/feedback/import
```

上传 CSV。

```text
GET
/projects/{project_id}/feedback
```

查看 Raw Feedback。

```text
POST
/projects/{project_id}/analysis
```

启动 AI Analysis。

然后：

```text
POST
/projects/{project_id}/clustering
```

运行聚类。

```text
GET
/projects/{project_id}/clusters
```

查看 Cluster。

```text
GET
/clusters/{cluster_id}
```

查看：

```text
Cluster
Insight
Evidence
```

这样前后端职责很清楚。

---

# 19. Agent Run 状态也要从第一天设计

用户点击：

**Analyze 500 Feedback**

不要页面卡住。

显示：

```text
Preparing Data
✓

Classifying Feedback
327 / 500

Generating Embeddings
...

Clustering
...

Generating Insights
...
```

内部状态：

```text
PENDING

RUNNING

COMPLETED

FAILED
```

这样以后 Demo 会舒服很多。

---

# 20. Sprint 1 页面实际上只做 4 个

不要现在做 8 个页面。

### Overview

```text
500 Feedback

21 Clusters

7 Major Issues

63% Negative
```

### Feedback

表格：

```text
Feedback
Type
Module
Intent
Severity
Sentiment
```

### Insights

类似：

```text
Search Accuracy          55
Memory                   35
Batch Image Upload       28
File Upload              27
...
```

### Insight Detail

这是重点页面：

```text
Batch Image Upload

28 feedback
21 users

Pain Point

...

Insight

...

Evidence

...

AI Confidence
```

Sprint 1 就够了。

---

# 21. 这一阶段我们的工程目录可以这么定

```text
insightflow/

frontend/
    app/
    components/
    lib/

backend/
    app/
        main.py

        api/
            projects.py
            feedback.py
            analysis.py
            clusters.py

        models/
            project.py
            feedback.py
            analysis.py
            cluster.py

        schemas/

        services/
            file_parser.py
            feedback_classifier.py
            embedding_service.py
            clustering_service.py
            insight_service.py

        agents/
            prompts/
                feedback_classification.py
                cluster_review.py
                insight_generation.py

        db/

        evals/

data/
    demo_feedback.csv
    golden_dataset.csv
```

这个结构后面扩展 Agent、Eval、PRD 都不会乱。

---

# 22. 我建议实际开发顺序

这次不要“前端页面全部做完再接 AI”。

正确顺序是：

```text
① PostgreSQL
       ↓
② Project + Feedback Table
       ↓
③ CSV Import API
       ↓
④ Classification Pipeline
       ↓
⑤ Embedding
       ↓
⑥ Clustering
       ↓
⑦ Insight Generation
       ↓
⑧ Evidence
       ↓
⑨ API验证
       ↓
⑩ 再接前端
```

原因是你的核心竞争力不在页面。

而在：

> **Feedback → Insight Pipeline**

前端最后再包起来。

---

# 23. Sprint 1 我们给自己定 5 个硬指标

完成后不能只说“能跑”。

至少要测：

| Metric | Sprint 1目标 |
|---|---:|
| Feedback Classification Accuracy | ≥ 90% |
| Intent Accuracy | ≥ 85% |
| Cluster Purity | ≥ 85% |
| Evidence Validity | ≥ 95% |
| Pipeline Success Rate | ≥ 95% |

暂时**不要硬追求这些数字一定达到**。

这是我们的目标，不是往简历上提前写的成绩。

最后简历必须写真实测出来的数据。

---

# 24. 到这里，InsightFlow 的第一个技术闭环已经确定

最终就是：

```text
Raw Feedback
      ↓
Structured Classification
      ↓
Semantic Representation
      ↓
Embedding
      ↓
Algorithmic Clustering
      ↓
LLM Semantic Refinement
      ↓
Product Insight
      ↓
Evidence Grounding
```

这里实际上已经包含了你以后面试时很好讲的四个 AI 产品设计思想：

**① Structured Output**  
避免不可控的自由文本输出。

**② Hybrid AI Pipeline**  
不是任何问题都交给 LLM，而是 Algorithm + LLM 协作。

**③ Evidence Grounding**  
AI 洞察必须能回溯真实用户反馈。

**④ Evaluation-driven Development**  
通过 Golden Dataset 判断 AI 是否真的改善，而不是凭感觉调 Prompt。

---

## 接下来正式进入“写项目”

下一步我建议我们直接做 **InsightFlow 后端 V0.1**，优先实现这三个东西：

```text
FastAPI Backend
        +
PostgreSQL 数据模型
        +
CSV Feedback Import
```

紧接着再接入第一个真正的 AI 能力：

```text
POST /analysis
      ↓
500 Feedback
      ↓
Structured Classification
```

也就是说，从下一步开始，我们已经不是“策划这个项目”了，而是**真正开始搭 InsightFlow 第一版代码骨架**。
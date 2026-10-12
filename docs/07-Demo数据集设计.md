# Demo 数据集设计

产品背景：虚构 AI Chat Assistant。模拟数据只用于演示与测试，不代表真实用户反馈、商业影响或已取得的模型效果。

## 两层数据

- data/demo_feedback.csv：本次提供12条可导入样本，不是完整500条。
- data/golden_dataset.csv：同12条样本的初始参考标签，由本次整理拟定，尚未人工复核，不是正式黄金评测集。
- 业务上传只接受原始数据字段，golden 文件不得交给分类模型或聚类算法作为输入。

## 500条正式数据的主题配额

原方案逐项相加为492。保留其余19项，建议 Response Quality 从55增加到63补足8条；这是本次修正建议，不伪装成原讨论结论。

| 主题 | 原数量 | 修正建议 |
|---|---:|---:|
| Response Quality |55|63|
| Search Accuracy |42|42|
| Response Speed |35|35|
| Memory |35|35|
| Subscription & Pricing |32|32|
| File Upload |30|30|
| Context Loss |30|30|
| Image Upload |28|28|
| Mobile Experience |28|28|
| Conversation Management |24|24|
| Voice |22|22|
| Model Availability |20|20|
| UI / Interaction |20|20|
| Multilingual |18|18|
| Web Browsing |18|18|
| Export / Sharing |18|18|
| Login / Account |14|14|
| Privacy |12|12|
| Positive Feedback |8|8|
| Other |3|3|
| 合计 |492|500|

来源配额：in_app175、support125、app_store75、community75、interview50。用户类型：free275、pro175、team50。此处比例是反馈条数占比，不声称独立用户占比。

用同义改写、隐含意图、多问题、mixed情绪、模糊句和离群项提升难度；用户ID允许重复，以区分feedback_count与unique_user_count；日期覆盖多个可比较时间窗。

20个主题包含正面与其他，不应全部称为20个“需求”。分类目标允许多种意图，聚类也不强制20簇。

完整评测集应有人工复核与歧义记录；将同义近重复样本放同一切分，防止泄漏。报告真实样本数、标注规则、模型与Prompt版本和运行日期；不照搬原讨论示例中的92%、83%等数字。

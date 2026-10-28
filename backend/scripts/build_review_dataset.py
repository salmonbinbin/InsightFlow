"""Reproducible synthetic workload, deliberately NOT a human-labelled golden set."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

TOPICS = [
    (
        "upload_multiple",
        [
            "希望选择多张图片后一次上传。",
            "每次只能选一张照片，处理整组素材要反复点击。",
            "能不能保留已选照片，再继续添加下一批？",
        ],
    ),
    (
        "upload_pdf",
        [
            "PDF上传到一半就停住，重新选择文件也没反应。",
            "同一份PDF在电脑能传，手机上却提示失败。",
            "上传PDF失败时没有说明具体原因。",
        ],
    ),
    (
        "search_relevance",
        [
            "搜索合同编号却出现了完全无关的聊天。",
            "想查上次的报销说明，结果大多不包含报销内容。",
            "用完整标题搜索也找不到保存过的资料。",
        ],
    ),
    (
        "response_speed",
        [
            "提交问题后要等很久才开始回答。",
            "长文总结一直显示处理中，不知道还要等多久。",
            "短问题的回复也经常停顿好几秒。",
        ],
    ),
    (
        "memory_accuracy",
        [
            "已经说过不要推荐付费方案，下一轮仍然推荐。",
            "每次新对话都要重新说明我的工作背景。",
            "助手把我临时举的例子当成了长期偏好。",
        ],
    ),
    (
        "history_organization",
        [
            "想按项目把历史对话放到不同文件夹。",
            "对话太多，找不到上周那一组讨论。",
            "希望给聊天加标签，方便以后集中查阅。",
        ],
    ),
    (
        "voice_recognition",
        [
            "语音输入把专业名词识别成了别的词。",
            "安静环境里说话，转成文字后仍缺了一整句。",
            "语音识别错了，修改文本的入口不容易找到。",
        ],
    ),
    (
        "subscription_price",
        [
            "偶尔用一次也要买整月订阅，价格不太合适。",
            "希望提供按次数购买的套餐。",
            "团队只有两个人，目前套餐起步席位太多。",
        ],
    ),
    (
        "subscription_cancel",
        [
            "找不到取消自动续费的位置。",
            "取消订阅后页面仍显示下个月扣费，不知道是否成功。",
            "希望取消续费时能明确告诉我权益到哪天结束。",
        ],
    ),
    (
        "mobile_keyboard",
        [
            "手机键盘弹出后挡住了发送按钮。",
            "输入长问题时文本框只有一行，很难检查前文。",
            "切换输入法后刚才写的内容消失了。",
        ],
    ),
    (
        "answer_citation",
        [
            "答案附的链接打开后没有对应的那句话。",
            "希望说明哪些段落来自资料，哪些是助手自己的推断。",
            "引用了过期文档，但没有告诉我发布日期。",
        ],
    ),
    (
        "table_output",
        [
            "生成的表格列错位，复制到表格软件后需要重新整理。",
            "长表格只输出了前半部分，没有说明被截断。",
            "希望表格可以直接下载为CSV。",
        ],
    ),
    (
        "export_history",
        [
            "想把某个项目的聊天记录完整导出。",
            "导出文件没有保留提问和回复的时间。",
            "需要交接给同事，但只能逐段复制历史内容。",
        ],
    ),
    (
        "accessibility",
        [
            "页面文字对比度太低，浅色提示很难看清。",
            "只用键盘操作时无法切换左侧项目。",
            "读屏软件没有读出上传按钮的名称。",
        ],
    ),
    (
        "sync_devices",
        [
            "电脑删掉的草稿在手机上又出现了。",
            "手机改完内容后，电脑一直显示旧版本。",
            "同时打开两个设备时不清楚哪份内容是最新的。",
        ],
    ),
    (
        "team_permission",
        [
            "希望分享链接能设置只读权限。",
            "撤回分享后原来的链接仍能访问。",
            "团队成员离开后，找不到统一移除访问权的位置。",
        ],
    ),
    (
        "positive_summary",
        [
            "这次会议总结保留了每个人的待办，节省了整理时间。",
            "摘要很清楚，能直接用来回顾讨论。",
            "分段总结很有帮助，我很容易找到重点。",
        ],
    ),
    (
        "positive_search",
        [
            "这次用关键词很快找到了需要的对话。",
            "搜索结果带原文片段，确认内容很方便。",
            "按时间筛选后定位到历史记录，比之前省事。",
        ],
    ),
    (
        "content_safety",
        [
            "助手把未经核实的消息写成确定事实，希望标出不确定性。",
            "询问资料来源时，助手重复了答案却没给出处。",
            "遇到没有资料的问题，希望直接说明不知道。",
        ],
    ),
    (
        "other_feedback",
        [
            "想知道下次版本更新会在什么时间。",
            "在哪里可以查看产品的使用教程？",
            "我想参加新功能内测，需要怎样报名？",
        ],
    ),
]
CONTEXTS = [
    "整理个人资料时",
    "准备课程材料时",
    "处理团队协作任务时",
    "在外出途中使用时",
    "完成周报时",
]
RAW_FIELDS = ["feedback_id", "content", "source", "user_id", "user_type", "created_at"]


def build(output: Path):
    output.mkdir(parents=True, exist_ok=False)
    rows, groups = [], []
    for topic_index, (topic, expressions) in enumerate(TOPICS):
        split = "train" if topic_index < 12 else "validation" if topic_index < 16 else "test"
        for context_index, context in enumerate(CONTEXTS):
            for expression_index, expression in enumerate(expressions):
                number = len(rows) + 1
                eid = f"SYN300-{number:03d}"
                content = f"{context}，{expression}"
                rows.append(
                    dict(
                        feedback_id=eid,
                        content=content,
                        source="synthetic_fixture",
                        user_id=f"SYN-USER-{(number - 1) % 75 + 1:03d}",
                        user_type="unknown",
                        created_at=f"2026-{9 + (number - 1) // 150:02d}-{(number - 1) % 28 + 1:02d}T08:00:00Z",
                    )
                )
                groups.append(
                    dict(
                        feedback_id=eid,
                        design_group=topic,
                        split=split,
                        content_hash=hashlib.sha256(content.encode()).hexdigest(),
                        selected_for_review=(context_index * 3 + expression_index) % 3 == 0,
                    )
                )

    def write_csv(name, fields, values):
        with (output / name).open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(values)

    write_csv("feedback.csv", RAW_FIELDS, rows)
    write_csv("groups.csv", list(groups[0]), groups)
    review_fields = [
        "feedback_id",
        "content",
        "content_hash",
        "review_status",
        "reviewer",
        "feedback_type",
        "severity",
        "cluster_label",
    ]
    by_id = {r["feedback_id"]: r for r in rows}
    review = [
        dict(
            feedback_id=g["feedback_id"],
            content=by_id[g["feedback_id"]]["content"],
            content_hash=g["content_hash"],
            review_status="pending",
            reviewer="",
            feedback_type="",
            severity="",
            cluster_label="",
        )
        for g in groups
        if g["selected_for_review"]
    ]
    write_csv("review-100.csv", review_fields, review)
    manifest = {
        "version": "synthetic-review-v1",
        "provenance": "synthetic",
        "label_status": "unreviewed",
        "feedback_count": len(rows),
        "review_candidate_count": len(review),
        "design_group_count": len(TOPICS),
        "split_counts": {
            s: sum(g["split"] == s for g in groups) for s in ("train", "validation", "test")
        },
        "split_policy": "whole design groups; no same group crosses splits",
        "limitation": "templated synthetic workload; design groups are not human ground truth; not semantic acceptance",
        "files": {
            name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in ("feedback.csv", "groups.csv", "review-100.csv")
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New directory; existing directory is never overwritten",
    )
    print(json.dumps(build(parser.parse_args().output), ensure_ascii=False, indent=2))

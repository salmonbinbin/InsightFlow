"""CSV ingestion preserves content verbatim; validation completes before DB writes."""

import csv
import io
import re
from datetime import datetime, timezone

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10000
FIELDS = {"feedback_id", "content", "source", "user_id", "user_type", "created_at"}


class CSVValidationError(ValueError):
    def __init__(self, message, row=None):
        self.detail = {"message": message, "row": row}
        super().__init__(message)


def parse_date(value):
    if not value:
        return None
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)?", value
    ):
        raise ValueError("invalid ISO date")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def parse_csv(data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CSVValidationError("请使用 UTF-8 编码的 CSV") from exc
    if "\x00" in text:
        raise CSVValidationError("CSV 不能包含 NUL 字符")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    rows = []
    seen = {}
    try:
        header = reader.fieldnames
        if not header or len(header) != len(set(header)):
            raise CSVValidationError("缺少表头或表头重复")
        if not {"feedback_id", "content"} <= set(header) or set(header) - FIELDS:
            raise CSVValidationError(
                "必须包含 feedback_id、content；其余列仅支持 source、user_id、user_type、created_at"
            )
        for row in reader:
            line = reader.line_num
            if len(rows) >= MAX_ROWS:
                raise CSVValidationError("单次最多导入 10000 条反馈", line)
            if None in row or any(v is None for v in row.values()):
                raise CSVValidationError("字段数量与表头不一致", line)
            external_id = row["feedback_id"].strip()
            content = row["content"]
            if not external_id or not content.strip():
                raise CSVValidationError("feedback_id 和 content 不能为空", line)
            if len(external_id) > 200 or len(content) > 20000:
                raise CSVValidationError("feedback_id 最长 200 字，content 最长 20000 字", line)
            item = {"external_id": external_id, "content": content}
            for field, limit in [("source", 100), ("user_id", 200), ("user_type", 100)]:
                value = row.get(field, "").strip() or None
                if value and len(value) > limit:
                    raise CSVValidationError(f"{field} 最长 {limit} 字", line)
                item[field] = value
            try:
                item["feedback_created_at"] = parse_date(row.get("created_at", "").strip())
            except ValueError as exc:
                raise CSVValidationError("created_at 必须为有效 ISO 日期或时间", line) from exc
            if external_id in seen and seen[external_id] != item:
                raise CSVValidationError("同一文件中存在相同 ID、不同内容的反馈", line)
            seen[external_id] = item
            rows.append(item)
    except csv.Error as exc:
        raise CSVValidationError("CSV 格式错误", reader.line_num) from exc
    if not rows:
        raise CSVValidationError("CSV 中没有反馈记录")
    return rows

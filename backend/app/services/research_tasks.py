"""Anchored task candidates, with server-owned omission accounting.

This is a representation contract, not a semantic completeness oracle. A
parseable plan only authorizes evidence evaluation. Every unaccounted clause
and every unsupported condition remains available for that evaluation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

KIND_UNRESOLVED = "unresolved_clause"
ROLE_CORE = "core"
ROLE_AUXILIARY = "auxiliary"


class TaskContractError(ValueError):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail[:240]


@dataclass(frozen=True)
class Subject:
    product: str | None = None
    platform: str | None = None
    entry: str | None = None


@dataclass(frozen=True)
class Constraint:
    kind: str
    value: str
    question_spans: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class TaskItem:
    id: str
    question_span: tuple[int, int]
    kind: str
    role: str
    subject: Subject
    action: str
    constraints: tuple[Constraint, ...]
    answer_mode: str = "explicit_official_evidence"


@dataclass(frozen=True)
class QuestionTaskPlan:
    original_question: str
    items: list[TaskItem]
    unresolved: list[TaskItem] = field(default_factory=list)
    has_full_question_mask: bool = False

    @property
    def is_completable(self) -> bool:
        """Parsing eligibility only; never evidence or answer completeness."""
        return (
            any(i.role == ROLE_CORE and i.kind != KIND_UNRESOLVED for i in self.items)
            and not self.unresolved
            and not self.has_full_question_mask
        )


_TASK_KEYS = {
    "id",
    "question_span",
    "kind",
    "role",
    "subject",
    "action",
    "constraints",
    "answer_mode",
}
_KINDS = {
    "capability",
    "procedure",
    "condition",
    "restriction",
    "limit",
    "entry_listing",
    "destination",
    "version",
    "comparison",
    "analysis",
    KIND_UNRESOLVED,
}
_ALIASES = {
    "安卓": "android",
    "苹果手机": "iphone",
    "手机": "phone",
    "平板": "tablet",
    "手动": "manual",
    "自动": "automatic",
    "备份": "backup",
    "上传": "upload",
    "选择": "select",
    "多张": "multiple",
    "多选": "multiple",
    "批量": "multiple",
    "应用": "app",
}
_ACTION_WORDS = {
    "backup": ("backup", "back up", "备份"),
    "upload": ("upload", "上传", "add photos", "添加照片"),
    "destination": ("where", "去向", "哪里", "何处", "上传到"),
    "choice": ("choices", "options", "选项", "入口"),
    "version": ("version", "ios", "android", "版本"),
}
_MULTIPLE = re.compile(r"\b(?:multiple|several|batch|multi[- ]select)\b|多张|多选|批量", re.I)
_CONDITION = re.compile(
    r"\b(?:while|when|if|unless|without|before|after|only|offline|disabled)\b|"
    r"离线|禁用|关闭|仅限|只有|除非|条件|没有订阅",
    re.I,
)
_SOURCE = re.compile(
    r"\b(?:according to|per)\s+[^,?;]+,\s*|\bcite only\b[^;?!.]*[.!?]?|"
    r"请?仅引用[^；。？！]*[。]?",
    re.I,
)
_FILLER = frozenset(
    "in the on an a of for to from by at with and or but can does do is it possible "
    "user users app documentation official help support according per google s "
    "please cite only show use using that this these which what how are be still 在 中".split()
)


def _normal(text: str) -> str:
    text = text.lower().replace("’", "'").replace("_", " ")
    for source, replacement in _ALIASES.items():
        text = text.replace(source, " " + replacement + " ")
    return re.sub(r"[^\w]+", " ", text.replace("'s", " ")).strip()


def _words(text: str) -> set[str]:
    return set(_normal(text).split())


def _span(raw: Any, question: str) -> tuple[int, int]:
    if not isinstance(raw, dict) or set(raw) != {"start", "end"}:
        raise TaskContractError("task_span_shape")
    a, b = raw["start"], raw["end"]
    if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(question):
        raise TaskContractError("task_span_range")
    return a, b


def _unresolved(question: str, span: tuple[int, int], index: int) -> TaskItem:
    return TaskItem(
        f"unresolved-{index}",
        span,
        KIND_UNRESOLVED,
        ROLE_CORE,
        Subject(),
        question[slice(*span)],
        (),
        "human_review_required",
    )


def build_task_plan(question: str, candidate_tasks: list[dict]) -> QuestionTaskPlan:
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        raise TaskContractError("task_question_invalid")
    if not isinstance(candidate_tasks, list) or len(candidate_tasks) > 16:
        raise TaskContractError("task_candidates_invalid")
    items: list[TaskItem] = []
    unresolved: list[TaskItem] = []
    accounted = [False] * len(question)
    for m in _SOURCE.finditer(question):
        accounted[m.start() : m.end()] = [True] * (m.end() - m.start())
    question_words = _words(question)
    for raw in candidate_tasks:
        if not isinstance(raw, dict) or set(raw) != _TASK_KEYS:
            raise TaskContractError("task_candidate_fields")
        span = _span(raw["question_span"], question)
        if span == (0, len(question)):
            raise TaskContractError("task_full_question_mask")
        anchor = question[slice(*span)]
        if re.match(r"\s*(?:cite only|仅引用|请仅引用)", anchor, re.I):
            raise TaskContractError("task_anchor_not_a_question")
        if raw["kind"] not in _KINDS or raw["role"] not in {ROLE_CORE, ROLE_AUXILIARY}:
            raise TaskContractError("task_kind_role_invalid")
        if raw["answer_mode"] not in {"explicit_official_evidence", "human_review_required"}:
            raise TaskContractError("task_answer_mode_invalid")
        if not isinstance(raw["id"], str) or not 1 <= len(raw["id"]) <= 100:
            raise TaskContractError("task_id_invalid")
        if any(i.id == raw["id"] for i in items):
            raise TaskContractError("task_id_duplicate")
        subject_raw = raw["subject"]
        if not isinstance(subject_raw, dict) or set(subject_raw) != {
            "product",
            "platform",
            "entry",
        }:
            raise TaskContractError("task_subject_fields")
        for key, value in subject_raw.items():
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 200):
                raise TaskContractError("task_subject_invalid")
            wanted = _words(value or "") - {"or", "app"}
            if wanted and not wanted <= question_words:
                raise TaskContractError("task_subject_not_in_question")
            if key == "entry" and value not in {None, "app"} and wanted:
                if not wanted <= _words(anchor):
                    raise TaskContractError("task_entry_not_in_anchor")
        action = raw["action"]
        if not isinstance(action, str) or not 1 <= len(action) <= 100:
            raise TaskContractError("task_action_invalid")
        for meaning, synonyms in _ACTION_WORDS.items():
            if meaning in action and not any(s in anchor.lower() for s in synonyms):
                raise TaskContractError("task_action_not_in_anchor")
        constraints_raw = raw["constraints"]
        if not isinstance(constraints_raw, list) or len(constraints_raw) > 20:
            raise TaskContractError("task_constraints_invalid")
        constraints = []
        for c in constraints_raw:
            if not isinstance(c, dict) or set(c) != {"kind", "value"}:
                raise TaskContractError("task_constraint_fields")
            if any(not isinstance(c[k], str) or not 1 <= len(c[k]) <= 200 for k in c):
                raise TaskContractError("task_constraint_invalid")
            if c["kind"] == "quantity" and c["value"] == "photos":
                continue  # An ordinary plural is never normalized into multiple.
            if c["kind"] == "quantity" and c["value"] not in {"multiple", "single", "unknown"}:
                raise TaskContractError("task_quantity_invalid")
            if (
                c["kind"] == "quantity"
                and c["value"] == "multiple"
                and not _MULTIPLE.search(question)
            ):
                raise TaskContractError("task_quantity_not_in_question")
            if c["kind"] not in {
                "quantity",
                "version",
                "mode",
                "device_scope",
                "distinguish_auto",
                "condition",
                "limit",
                "entry",
                "platform",
                "unknown",
            }:
                raise TaskContractError("task_constraint_kind_invalid")
            matches = (
                tuple(m.span() for m in _MULTIPLE.finditer(question))
                if c["kind"] == "quantity" and c["value"] == "multiple"
                else tuple(
                    m.span()
                    for m in re.finditer(re.escape(c["value"].replace("_", " ")), question, re.I)
                )
            )
            constraints.append(Constraint(c["kind"], c["value"], matches))
        item = TaskItem(
            raw["id"],
            span,
            raw["kind"],
            raw["role"],
            Subject(**subject_raw),
            action,
            tuple(constraints),
            raw["answer_mode"],
        )
        items.append(item)
        accounted[span[0] : span[1]] = [True] * (span[1] - span[0])
        # Subject words outside the action anchor remain tied to the original
        # question. This accounts for syntax such as "In <product> <platform>".
        subject_words = set().union(*(_words(v or "") for v in subject_raw.values()))
        for m in re.finditer(r"[\w]+", question):
            if (_words(m.group()) - _FILLER) <= subject_words:
                accounted[m.start() : m.end()] = [True] * (m.end() - m.start())
        if any(c.kind == "distinguish_auto" for c in constraints):
            for m in re.finditer(
                r"(?:separate|distinguish)[^.;？！。]*|[^；。？！]*?(?:分开说明|区分)[^；。？！]*",
                question,
                re.I,
            ):
                accounted[m.start() : m.end()] = [True] * (m.end() - m.start())
    # Every uncovered substantive token is retained verbatim, including unknown
    # conditions. No product list or fixed question template determines this.
    for m in re.finditer(r"[\w]+", question):
        if all(accounted[m.start() : m.end()]):
            continue
        if not (_words(m.group()) - _FILLER):
            continue
        unresolved.append(_unresolved(question, m.span(), len(unresolved) + 1))
    # A broad action span cannot hide a condition. Even known conditions remain
    # a separate core item until a later, independent evidence predicate proves
    # their full clause. Merely naming a constraint does not prove its meaning.
    for m in _CONDITION.finditer(question):
        if _SOURCE.match(question, m.start()):
            continue
        if (
            m.group().lower() == "only"
            and "cite" in question[max(0, m.start() - 8) : m.start()].lower()
        ):
            continue
        end = re.search(r"[;?.!。？！；]", question[m.end() :])
        b = m.end() + end.start() if end else len(question)
        unresolved.append(_unresolved(question, (m.start(), max(m.end(), b)), len(unresolved) + 1))
    if not candidate_tasks:
        unresolved = [_unresolved(question, (0, len(question)), 1)]
        items = list(unresolved)
    plan = QuestionTaskPlan(question, items, unresolved)
    validate_task_plan(plan)
    return plan


def validate_task_plan(plan: QuestionTaskPlan) -> None:
    if not isinstance(plan, QuestionTaskPlan) or not plan.original_question:
        raise TaskContractError("task_plan_invalid")
    for item in plan.items + plan.unresolved:
        a, b = item.question_span
        if not 0 <= a < b <= len(plan.original_question):
            raise TaskContractError("task_span_range")
        if item.role not in {ROLE_CORE, ROLE_AUXILIARY}:
            raise TaskContractError("task_role_invalid")

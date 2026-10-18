"""Bounded exact fragments for every body line, including long steps/conditions.

Sections and offsets use the existing catalog grammar. Long original lines are
partitioned without truncation, unescaping or word rewriting. A fragment never
silently stands in for the rest of its original line.

Y3-flow: numbered lists are split by NUMBERING rollback, not by paragraph
separators. ``1, 2, 1, 2`` under one heading is two flows (the second ``1``
restarts); ``1, 2, 3`` with an inter-step Note or pre-condition paragraph is
still one flow (no rollback). Each flow keeps its own ``flow_step_total``
(the max step_index in the list) and a stable ``flow_id`` keyed on
``snapshot_hash : section_start : list_start``.

Non-numbered descriptions (pre-conditions, inter-step Notes, end-of-list
branches) keep their legal scope: when a section has only ONE numbered list,
every non-numbered line under that heading inherits that flow's identity so
a frozen pre-condition / Note never loses its binding to the only flow. When
a section has multiple numbered lists, only descriptions that fall WITHIN a
list's numbered span (``first_off <= line_off <= last_off``) get attributed
— descriptions before the first list, in the gap between two lists, or
after the last list stay unattributed rather than being arbitrarily
assigned to a sibling flow.

Flow metadata is sourced strictly from the current section: cross-section
flows do not pollute each other's attribution context.
"""

import re
from dataclasses import replace

from app.services.research_contract import build_evidence_catalog, build_evidence_unit


def line_span(snapshot, offset):
    start = snapshot.rfind("\n", 0, offset) + 1
    end = snapshot.find("\n", offset)
    return start, len(snapshot) if end < 0 else end


def branch_span(snapshot, offset):
    a, b = line_span(snapshot, offset)
    markers = [a] + [
        a + m.start()
        for m in re.finditer(r"\b(?:Tip|Note|Important):\s*", snapshot[a:b])
        if m.start()
    ]
    left = max(x for x in markers if x <= offset)
    right = min([x for x in markers if x > offset] or [b])
    return left, right


_NUMBERED_HEAD = re.compile(r"^\s*(\d+)[.)]\s+")


def _list_boundaries(cat, body: str):
    """Y3-flow: detect numbered-list boundaries via NUMBERING rollback.

    Returns four parallel structures:
    - ``list_lines[line_offset]`` → the ``list_start_offset`` for each
      numbered line.
    - ``list_max_index[list_start_offset]`` → max step_index in that
      list (``flow_step_total``).
    - ``list_first_item[list_start_offset]`` → offset of the FIRST
      numbered item in that list.
    - ``list_last_item[list_start_offset]`` → offset of the LAST
      numbered item in that list (start of that line). Used by
      :func:`_attribute_flow` to fence each list's numbered span so a
      description only attributes when it falls strictly between the
      first and last numbered lines of one flow (gaps and pre/post
      unknown descriptions stay unattributed).

    A new list starts at a numbered line when:
    - no list is currently open in the section, OR
    - the new ``step_index`` is ``<=`` the previous numbered item's
      ``step_index`` (rollback or restart). Equal (e.g. ``1, 1``) and
      descending (``2, 1``) both count as restart; ascending past the
      previous index (``1, 3``) keeps the list open even when numbers
      are missing in between — the procedure check rejects the gap.

    Section transitions always close the open list; two sections cannot
    share a flow identity.
    """
    list_lines: dict[int, int] = {}
    list_max_index: dict[int, int] = {}
    list_first_item: dict[int, int] = {}
    list_last_item: dict[int, int] = {}
    prev_sec = None
    current_list_start: int | None = None
    current_list_max = 0
    last_step_index: int | None = None
    offset = 0
    for line in body.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        sec = next(
            (s for s in cat.sections if s.start_offset <= offset < s.end_offset),
            None,
        )
        if sec is not prev_sec:
            if current_list_start is not None:
                list_max_index[current_list_start] = current_list_max
            current_list_start = None
            current_list_max = 0
            last_step_index = None
            prev_sec = sec
        if sec is None:
            offset += len(line)
            continue
        m = _NUMBERED_HEAD.match(raw)
        if m:
            step_index = int(m.group(1))
            if (
                current_list_start is None
                or last_step_index is None
                or step_index <= last_step_index
            ):
                # Close any open list (rollover / restart → new flow).
                if current_list_start is not None:
                    list_max_index[current_list_start] = current_list_max
                current_list_start = offset
                current_list_max = 0
                list_first_item.setdefault(current_list_start, offset)
                list_last_item.setdefault(current_list_start, offset)
            else:
                list_first_item.setdefault(current_list_start, current_list_start)
                list_last_item[current_list_start] = offset
            current_list_max = max(current_list_max, step_index)
            list_lines[offset] = current_list_start
            last_step_index = step_index
        # Non-numbered lines never close the list; they keep the
        # current list open so an inter-step Note or pre-condition
        # paragraph stays in the same flow (K=1 sections inherit all
        # such lines; K>=2 sections rely on :func:`_attribute_flow` to
        # fence the gap descriptions as unattributed).
        offset += len(line)
    if current_list_start is not None:
        list_max_index[current_list_start] = current_list_max
    return list_lines, list_max_index, list_first_item, list_last_item


def _flow_id(cat, section_start: int, list_start: int) -> str:
    """Y2-flow: stable per-list identity. Snapshot hash anchors the
    body bytes, the section start isolates headings, the list start
    isolates parallel numbered lists under the same heading. Two
    snapshots of the same body share the same ``flow_id`` so the
    procedure check is reproducible.
    """
    return f"{cat.snapshot_sha256[:12]}:{section_start}:{list_start}"


def _attribute_flow(line_offset: int, sec, list_first_item, list_last_item):
    """Y3-flow: pick the flow_id for a non-numbered line.

    - Section with K == 1 list: the line inherits that single flow so
      pre-conditions, inter-step Notes and end-of-list branches keep
      their legal binding. The lone flow covers every non-numbered
      line in the section regardless of position — a frozen Dropbox
      pre-condition or a trailing OneDrive Note must not lose their
      flow identity to the procedure check.
    - Section with K >= 2 lists: only descriptions that fall WITHIN a
      flow's numbered span (``list_first_item[ls] <= line_offset <=
      list_last_item[ls]``) attribute to that flow. Lines BEFORE the
      first flow (could be a pre-condition for either method), in the
      GAP between two flows (a transition note that is clearly past
      the previous list's last step), or AFTER the last flow stay
      unattributed. ``list_last_item`` fences the gap so we never
      silently splice a sibling flow's transition / boundary
      description into the wrong flow.

    ``starts`` is filtered by section range (``sec.start <= first <
    sec.end``) so flows from earlier or later sections never pollute
    the current section's attribution context (a K=1 section must
    not see a neighbour's list as a sibling, which would otherwise
    lift it into a K>=2 grouping and split its pre-condition /
    Note across ghost flows).
    """
    sec_start = sec.start_offset
    sec_end = sec.end_offset
    starts = sorted(ls for ls, first in list_first_item.items() if sec_start <= first < sec_end)
    if not starts:
        return None
    if len(starts) == 1:
        return starts[0]
    for ls in starts:
        first_off = list_first_item[ls]
        last_off = list_last_item[ls]
        if first_off <= line_offset <= last_off:
            return ls
    return None


def build_catalogs(sources):
    catalogs = {}
    entries = {}
    for source in sources:
        body = source.snippet or ""
        cat = build_evidence_catalog(source.provider_source_id, body)
        catalogs[source.provider_source_id] = cat
        (
            list_lines,
            list_max_index,
            list_first_item,
            list_last_item,
        ) = _list_boundaries(cat, body)
        offset = 0
        for line in body.splitlines(keepends=True):
            raw = line.rstrip("\r\n")
            sec = next((s for s in cat.sections if s.start_offset <= offset < s.end_offset), None)
            if raw.strip() and not raw.lstrip().startswith("#"):
                step = _NUMBERED_HEAD.match(raw)
                marks = (
                    [0]
                    + [
                        m.start()
                        for m in re.finditer(r"\b(?:Tip|Note|Important):\s*", raw)
                        if m.start()
                    ]
                    + [len(raw)]
                )
                # Y2-flow: numbered lines carry their list's flow_id
                # directly; non-numbered lines inherit via the section
                # attribution helper so a single-flow section never
                # loses its pre-condition / Note / branch.
                line_flow_id: str | None = None
                line_flow_total: int | None = None
                if step and offset in list_lines:
                    list_start = list_lines[offset]
                    line_flow_id = _flow_id(cat, sec.start_offset, list_start)
                    line_flow_total = list_max_index.get(list_start)
                elif sec is not None:
                    attributed = _attribute_flow(offset, sec, list_first_item, list_last_item)
                    if attributed is not None:
                        line_flow_id = _flow_id(cat, sec.start_offset, attributed)
                        line_flow_total = list_max_index.get(attributed)
                for branch_left, branch_right in zip(marks, marks[1:]):
                    numbered = step if branch_left == 0 else None
                    segment = raw[branch_left:branch_right]
                    ranges = (
                        [(branch_left, branch_right)]
                        if numbered
                        else [
                            (branch_left + m.start(), branch_left + m.end())
                            for m in re.finditer(r".+?(?:[.!?](?=\s|$)|$)", segment)
                        ]
                    )
                    for left, right in ranges:
                        while left < right and raw[left].isspace():
                            left += 1
                        # Short colon-only scaffolding is document structure,
                        # not an independently supported factual assertion.
                        if len(raw[left:right].strip()) < 20 and raw[left:right].rstrip().endswith(
                            ":"
                        ):
                            continue
                        for start in range(left, right, 200):
                            length = min(200, right - start)
                            u = build_evidence_unit(
                                snapshot=body,
                                source_id=source.provider_source_id,
                                offset=offset + start,
                                length=length,
                                section=sec.title if sec else "",
                                context=" / ".join(sec.full_path) if sec else "",
                            )
                            u = replace(
                                u,
                                section_path=sec.full_path if sec else (),
                                step_index=int(numbered[1]) if numbered else 0,
                                step_total=sec.step_total if numbered and sec else 0,
                                catalog_id=f"{source.provider_source_id}@{u.offset}+{u.length}",
                            )
                            entries[u.evidence_id] = {
                                "unit": u,
                                "source": source,
                                "statement_offset": offset + left,
                                "statement_length": right - left,
                                "flow_id": line_flow_id,
                                "flow_step_total": line_flow_total,
                            }
            offset += len(line)
    return catalogs, entries

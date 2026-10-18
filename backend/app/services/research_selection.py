"""Deterministic evidence locations and closed v5 selection materialization."""

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any, TypedDict

from app.services.research_contract import (
    Catalog,
    EvidenceUnit,
    build_evidence_catalog,
    build_evidence_unit,
)


class SelectionError(ValueError):
    pass


class EvidenceEntry(TypedDict):
    unit: EvidenceUnit
    source: Any


def build_catalogs(sources: Iterable[Any]) -> tuple[dict[str, Catalog], dict[str, EvidenceEntry]]:
    catalogs = {}
    entries = {}
    for s in sources:
        body = s.snippet or ""
        cat = build_evidence_catalog(s.provider_source_id, body)
        catalogs[s.provider_source_id] = cat
        for u in cat.units:
            entries[u.evidence_id] = {"unit": u, "source": s}
        offset = 0
        for line in body.splitlines(keepends=True):
            raw = line.rstrip("\r\n")
            if (
                raw.strip()
                and not raw.lstrip().startswith("#")
                and not re.match(r"^\s*\d+[.)]", raw)
            ):
                # Save bounded exact sentence spans, never join or unescape text.
                # Non-greedy ``.`` so a sentence with internal periods
                # (e.g. ``iOS 16.1``, ``example.com``) is consumed as a
                # single span up to the next sentence-end punctuation,
                # instead of being restarted after the inner period and
                # losing the prefix.
                for m in re.finditer(r".+?(?:[.!?](?=\s|$)|$)", raw):
                    a, b = m.span()
                    while a < b and raw[a].isspace():
                        a += 1
                    if 0 < b - a <= 200:
                        sec = next(
                            (
                                x
                                for x in cat.sections
                                if x.start_offset <= offset + a < x.end_offset
                            ),
                            None,
                        )
                        u = build_evidence_unit(
                            snapshot=body,
                            source_id=s.provider_source_id,
                            offset=offset + a,
                            length=b - a,
                            section=sec.title if sec else "",
                            context=" / ".join(sec.full_path) if sec else "",
                        )
                        if sec is not None:
                            u = replace(
                                u,
                                section_path=sec.full_path,
                                catalog_id=f"{s.provider_source_id}@{u.offset}+{u.length}",
                            )
                        if not any(
                            e["unit"].offset == u.offset and e["unit"].length == u.length
                            for e in entries.values()
                            if e["source"].provider_source_id == s.provider_source_id
                        ):
                            entries[u.evidence_id] = {"unit": u, "source": s}
            offset += len(line)
    return catalogs, entries


def materialize(
    payload: Any, entries: Mapping[str, EvidenceEntry], product: str | None
) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(payload, dict) or set(payload) != {"summary", "confidence", "evidence_ids"}:
        raise SelectionError("selection_schema_invalid")
    summary = payload["summary"]
    confidence = payload["confidence"]
    ids = payload["evidence_ids"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 600:
        raise SelectionError("summary_invalid")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise SelectionError("confidence_invalid")
    if (
        not isinstance(ids, list)
        or len(ids) > 40
        or any(not isinstance(i, str) or i not in entries for i in ids)
    ):
        raise SelectionError("evidence_id_unknown")
    if len(ids) != len(set(ids)):
        raise SelectionError("evidence_id_duplicate")
    competitors = []
    for i in ids:
        unit = entries[i]["unit"]
        s = entries[i]["source"]
        # Re-read the saved snapshot. An id never authorizes arbitrary model text.
        text = (s.snippet or "")[unit.offset : unit.offset + unit.length]
        if text != unit.text:
            raise SelectionError("snapshot_drift")
        competitors.append(
            {
                "name": product or (s.title or s.domain)[:200],
                "description": f'Evidence: "{text}"',
                "confidence": confidence,
                "source_refs": [
                    {
                        "provider_source_id": s.provider_source_id,
                        "url": s.url,
                        "title": s.title or s.domain,
                    }
                ],
            }
        )
    return {
        "summary": summary,
        "confidence": confidence,
        "competitors": competitors,
        "market_patterns": [],
        "opportunities": [],
        "risks": [],
    }, ids

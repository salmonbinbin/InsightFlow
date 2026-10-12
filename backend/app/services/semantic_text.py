"""Phase 2 — deterministic ``semantic_text`` builder.

The text combines the raw feedback with the structured Phase 1 fields so the
embedding sees standardised vocabulary instead of short colloquial sentences.
The function is pure (no I/O) so the same feedback + analysis always yields
the same string; this is required so embedding caches and migrations are
stable across runs.
"""

from __future__ import annotations


def _normalize(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def build_semantic_text(
    content: str,
    *,
    module: str,
    intent: str,
    pain_point: str,
) -> str:
    """Return the deterministic embedding text for a feedback analysis row.

    The output contains the original feedback exactly as stored (whitespace
    collapsed) plus the structured fields produced by the Phase 1
    classification prompt. No new business judgement is added; the structured
    fields are written by Phase 1 and only replayed here.
    """
    parts: list[str] = [
        "Original feedback:",
        _normalize(content),
        "",
        "Module:",
        _normalize(module),
        "",
        "Intent:",
        _normalize(intent),
        "",
        "Pain Point:",
        _normalize(pain_point),
    ]
    return "\n".join(parts).strip() + "\n"


__all__ = ["build_semantic_text"]

"""Independent regressions using frozen public source snapshots."""

import json
from pathlib import Path
from types import SimpleNamespace

from app.services import research_service as api
from app.services.research_pipeline import _queries
from app.services.research_selection import build_catalogs

FIXTURES = Path(__file__).parent / "fixtures" / "phase8_research_v5"


def test_apple_shared_library_destination_with_decimal_version_is_an_exact_unit():
    data = json.loads((FIXTURES / "case-9.json").read_text())
    sources = [SimpleNamespace(**s) for s in data["sources"]]
    _, entries = build_catalogs(sources)
    line = next(
        line
        for s in sources
        if "mmb5dbaf376f" in s.url
        for line in s.snippet.splitlines()
        if line.startswith("Note: If you want to upload")
    )
    assert any(v["unit"].text == line for v in entries.values())


def test_sentence_catalog_preserves_embedded_hostname_and_version():
    body = "# Help\nUpload to example.com on iOS 16.1, then choose Photos.\n"
    source = SimpleNamespace(provider_source_id="s", snippet=body)
    _, entries = build_catalogs([source])
    assert any(
        v["unit"].text == "Upload to example.com on iOS 16.1, then choose Photos."
        for v in entries.values()
    )


def test_followup_query_always_contains_task_terms():
    q = json.loads((FIXTURES / "case-9.json").read_text())["question"]
    context = SimpleNamespace(research_question=q, scope="official help")
    rule = api._detect_official_vendor_rule(q, context.scope)
    queries = _queries(api, context, "iCloud.com upload site:support.apple.com/en-us", rule)
    assert queries
    for query in queries:
        assert any(not word.startswith("site:") for word in query.split()), query

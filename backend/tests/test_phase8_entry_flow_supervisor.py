"""Entry scope follows a proven list, never a sibling list or another source."""

from types import SimpleNamespace

from app.services.research_catalog_v7 import build_catalogs
from app.services.research_pipeline_v7 import _offer
from app.services.research_support_v7 import applicability


def catalog(body):
    source = SimpleNamespace(
        provider_source_id="s",
        url="https://help.dropbox.com/create-upload/upload-mobile",
        title="Dropbox mobile uploads",
        domain="help.dropbox.com",
        snippet=body,
    )
    return build_catalogs([source])[1]


def requirement(entry="Dropbox mobile app"):
    return dict(
        id="r",
        products=["Dropbox"],
        kind="procedure",
        platform="mobile",
        entry=entry,
        action="upload",
        object="photos",
        mode="manual",
        question_text="How to manually upload photos?",
    )


BODY = """# Dropbox mobile uploads
## Upload photos
If camera uploads are disabled, you can manually add photos.
1. Open the Dropbox mobile app.
2. Choose the photos.
3. Tap Upload.
"""


def test_offer_contains_entire_flow_and_precondition_with_entry_in_first_step():
    entries = catalog(BODY)
    offered = _offer({"requirements": [requirement()]}, entries, {}, 60)
    assert len(offered) == 4
    assert {e["unit"].step_index for e in offered.values()} == {0, 1, 2, 3}


def test_entry_cannot_be_borrowed_from_parallel_list():
    entries = catalog(BODY + "1. Open the Other tab.\n2. Tap Upload.\n")
    peers = [e["unit"] for e in entries.values()]
    target = list(entries.values())[-1]
    assert (
        applicability(requirement(), target["unit"], target["source"], peers, entries=entries)
        == "entry_conflict"
    )


def test_platform_conflict_still_wins_over_inherited_entry():
    entries = catalog(BODY + "Note: On Windows, video uploads are not supported.\n")
    peers = [e["unit"] for e in entries.values()]
    target = list(entries.values())[-1]
    assert (
        applicability(requirement(), target["unit"], target["source"], peers, entries=entries)
        is not None
    )

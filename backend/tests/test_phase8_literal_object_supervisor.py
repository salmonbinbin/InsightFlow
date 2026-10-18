"""Literal format qualifiers stay in the original obligation and source proof."""

import pytest

from app.services.research_input_v7 import extract_input_obligations
from app.services.research_spec_v7 import SpecError, compile_spec, question_parts
from app.services.research_support_v7 import _object_present


def candidate(q, obj):
    return dict(
        requirements=[
            dict(
                id="r1",
                anchor_ids=[a["id"] for a in question_parts(q)["anchors"]],
                products=["OneDrive"],
                kind="capability",
                platform="iPhone",
                entry="app",
                action="upload",
                object=obj,
                mode="unspecified",
                quantity="unspecified",
                condition_quotes=[],
                version=None,
                analysis_facets=[],
            )
        ],
        queries=[],
    )


@pytest.mark.parametrize("format_name", ["PDF", "QXZ"])
def test_literal_format_file_phrase_is_not_a_product_answer_vocabulary(format_name):
    q = f"Can OneDrive iPhone upload {format_name} files?"
    obligations = extract_input_obligations(q, anchor_list=question_parts(q)["anchors"])
    assert obligations[0]["object_text"] == format_name + " files"
    spec = compile_spec(q, candidate(q, format_name + " files"))
    assert not spec["unresolved"]
    assert spec["requirements"][0]["object"] == format_name + " files"


def test_generic_files_cannot_silently_answer_literal_subtype_question():
    q = "Can OneDrive iPhone upload PDF files?"
    with pytest.raises(SpecError, match="object_replaced"):
        compile_spec(q, candidate(q, "files"))


def test_other_format_cannot_satisfy_pdf_object_proof():
    from types import SimpleNamespace

    r = {"object": "PDF files"}
    assert not _object_present(
        r, [SimpleNamespace(text="Upload TXT files.", section="Upload files")]
    )
    assert _object_present(r, [SimpleNamespace(text="Upload PDF files.", section="Upload files")])

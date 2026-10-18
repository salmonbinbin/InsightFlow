"""Explicit project context must guard every artifact route before side effects."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_phase5 import (
    _http_seed,
    _StubPRDClient,
    _StubRequirementClient,
    client,
    engine,
)

from app.models import Project
from app.models.requirement_prd import PRD, Requirement
from app.services import requirement_prd as service

# Re-export fixtures only; no edits to protected Phase5 tests.
__all__ = ["client", "engine"]


def setup_artifacts(client, engine, monkeypatch):
    monkeypatch.setattr(service, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service, "PRDClient", _StubPRDClient)
    a, insight = _http_seed(engine)
    _, unused = _http_seed(engine)
    # Existing stub contract follows legacy prompts intentionally.
    req = client.post(f"/insights/{insight}/requirement").json()
    prd = client.post(f"/requirements/{req['id']}/prds").json()
    with Session(engine) as db:
        b = Project(name="Other context", product_name="B")
        db.add(b)
        db.commit()
        bid = b.id
    return a, bid, insight, unused, req["id"], prd["id"]


@pytest.mark.parametrize("case", range(8))
def test_foreign_project_is_404_and_no_side_effects(client, engine, monkeypatch, case):
    a, b, insight, unused, rid, pid = setup_artifacts(client, engine, monkeypatch)
    routes = [
        ("post", f"/insights/{unused}/requirement", {}),
        ("get", f"/requirements/{rid}", None),
        ("patch", f"/requirements/{rid}", dict(status="reviewing")),
        ("post", f"/requirements/{rid}/prds", {}),
        ("get", f"/prds/{pid}", None),
        ("patch", f"/prds/{pid}", dict(status="reviewing")),
        ("post", f"/prds/{pid}/accept", {}),
        ("get", f"/requirements/{rid}/prds", None),
    ]

    def snapshot():
        with Session(engine) as db:
            return (
                [
                    dict(
                        id=str(r.id),
                        status=r.status,
                        next_version=r.next_version,
                        problem=r.problem,
                    )
                    for r in db.scalars(select(Requirement))
                ],
                [
                    dict(
                        id=str(p.id),
                        status=p.status,
                        content=p.content,
                        accepted_at=str(p.accepted_at),
                    )
                    for p in db.scalars(select(PRD))
                ],
            )

    before = snapshot()
    calls = (len(_StubRequirementClient.calls), len(_StubPRDClient.calls))
    method, path, body = routes[case]
    kw = {} if body is None else dict(json=body)
    response = getattr(client, method)(path + f"?project_id={b}", **kw)
    assert response.status_code == 404, response.text
    assert snapshot() == before
    assert calls == (len(_StubRequirementClient.calls), len(_StubPRDClient.calls))


@pytest.mark.parametrize("context", ["correct", "omitted"])
def test_matching_and_legacy_context_remain_usable(client, engine, monkeypatch, context):
    a, _, _, _, rid, pid = setup_artifacts(client, engine, monkeypatch)
    suffix = f"?project_id={a}" if context == "correct" else ""
    for path in [f"/requirements/{rid}", f"/prds/{pid}", f"/requirements/{rid}/prds"]:
        assert client.get(path + suffix).status_code == 200
    assert (
        client.patch(f"/requirements/{rid}" + suffix, json=dict(status="reviewing")).status_code
        == 200
    )
    assert client.patch(f"/prds/{pid}" + suffix, json=dict(status="reviewing")).status_code == 200
    assert client.post(f"/prds/{pid}/accept" + suffix, json={}).status_code == 200


@pytest.mark.parametrize("path", ["/requirements/ID", "/prds/ID", "/requirements/ID/prds"])
def test_unknown_resource_stays_404(client, path):
    assert (
        client.get(path.replace("ID", str(uuid4())) + f"?project_id={uuid4()}").status_code == 404
    )

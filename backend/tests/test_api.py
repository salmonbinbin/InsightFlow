import csv
import io
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app

HEADER = ["feedback_id", "content", "source", "user_id", "user_type", "created_at"]


def csv_data(rows):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(HEADER)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


@pytest.fixture
def client(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.session import get_db
    from app.models import Base

    engine = create_engine(os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/test.db"))
    Base.metadata.create_all(engine)

    def override():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    engine.dispose()


def project(client):
    response = client.post("/projects", json={"name": "测试项目", "product_name": "AI Chat"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(client, pid, rows):
    return client.post(
        f"/projects/{pid}/feedback/import", files={"file": ("test.csv", csv_data(rows), "text/csv")}
    )


def test_roundtrip_and_idempotency(client):
    pid = project(client)
    rows = [["F1", "  照片,需要多选\n第二行  ", "support", "U1", "pro", "2026-10-01"]]
    first = upload(client, pid, rows)
    assert first.status_code == 200, first.text
    assert first.json() == {"total": 1, "imported": 1, "skipped": 0}
    assert upload(client, pid, rows).json()["skipped"] == 1
    data = client.get(f"/projects/{pid}/feedback").json()
    assert data["total"] == 1
    assert data["items"][0]["content"] == rows[0][1]
    assert data["items"][0]["external_id"] == "F1"
    assert data["items"][0]["feedback_created_at"].startswith("2026-10-01T00:00:00")
    assert client.get(f"/projects/{pid}").json()["product_name"] == "AI Chat"


def test_conflict_rolls_back_whole_import(client):
    pid = project(client)
    assert upload(client, pid, [["F1", "原文", "", "", "", ""]]).status_code == 200
    result = upload(
        client, pid, [["F2", "新内容", "", "", "", ""], ["F1", "被改写", "", "", "", ""]]
    )
    assert result.status_code == 409
    assert client.get(f"/projects/{pid}/feedback").json()["total"] == 1


@pytest.mark.parametrize(
    "rows",
    [
        [["F1", "合法", "", "", "", ""], ["F2", "   ", "", "", "", ""]],
        [["F1", "文字", "", "", "", "昨天"]],
        [["F1", "a", "", "", "", ""], ["F1", "b", "", "", "", ""]],
        [["F1", "a", "", "", "", "", "多余字段"]],
    ],
)
def test_invalid_rows_are_atomic(client, rows):
    pid = project(client)
    assert upload(client, pid, rows).status_code == 422
    assert client.get(f"/projects/{pid}/feedback").json()["total"] == 0


@pytest.mark.parametrize(
    "body",
    [
        b"content\nhello\n",
        b"feedback_id,content\n",
        b"feedback_id,content,content\n1,a,b",
        b"\xff\xfe",
        b'feedback_id,content\nF1,"unclosed',
    ],
)
def test_invalid_file(client, body):
    pid = project(client)
    result = client.post(
        f"/projects/{pid}/feedback/import", files={"file": ("x.csv", body, "text/csv")}
    )
    assert result.status_code == 422


def test_isolation_pagination_and_missing_project(client):
    p1, p2 = project(client), project(client)
    assert (
        upload(client, p1, [["F1", "甲", "", "", "", ""], ["F2", "乙", "", "", "", ""]]).status_code
        == 200
    )
    assert upload(client, p2, [["F1", "丙", "", "", "", ""]]).status_code == 200
    data = client.get(f"/projects/{p1}/feedback?limit=1&offset=1").json()
    assert data["total"] == 2 and len(data["items"]) == 1
    assert client.get(f"/projects/{p2}/feedback").json()["total"] == 1
    assert client.get(f"/projects/{p1}/feedback?limit=0").status_code == 422
    assert client.get(f"/projects/{uuid4()}/feedback").status_code == 404
    assert client.get("/projects/not-a-uuid/feedback").status_code == 422


def test_limits_and_filename(client):
    pid = project(client)
    for filename, data, expected in [
        ("x.xlsx", b"text", 415),
        ("x.csv", b"x" * (10 * 1024 * 1024 + 1), 413),
    ]:
        assert (
            client.post(
                f"/projects/{pid}/feedback/import", files={"file": (filename, data)}
            ).status_code
            == expected
        )
    rows = [[f"F{i}", "内容", "", "", "", ""] for i in range(10001)]
    assert upload(client, pid, rows).status_code == 422


def test_duplicate_within_file_and_optional_fields(client):
    pid = project(client)
    rows = [["F1", "相同", "", "", "", ""]] * 2
    assert upload(client, pid, rows).json() == {"total": 2, "imported": 1, "skipped": 1}
    result = client.post(
        f"/projects/{pid}/feedback/import",
        files={"file": ("x.csv", b"feedback_id,content\nF2,hello\n")},
    )
    assert result.status_code == 200


def test_project_validation_and_health(client):
    assert client.post("/projects", json={"name": "  ", "product_name": "A"}).status_code == 422
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/health/ready").status_code == 200

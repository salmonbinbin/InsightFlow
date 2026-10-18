import httpx
import pytest

from app.services import research_reader as reader


def test_body_budget_stops_while_receiving():
    chunks = []

    class LargeBody(httpx.SyncByteStream):
        def __iter__(self):
            for i in range(10):
                chunks.append(i)
                yield b"x" * (reader.MAX_BYTES // 2)

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/html"}, stream=LargeBody()
            )
        )
    ) as client:
        with pytest.raises(reader.ReadError):
            reader.read_official(
                "https://support.google.com/photos/answer/1", lambda url: True, client=client
            )
    assert len(chunks) <= 3

import pytest
from starlette.exceptions import HTTPException
from starlette.types import Message, Receive, Scope, Send

from invoice_agent.api.limits import BodyLimit

LIMIT = 1000


def scope(*, method: str = "POST", path: str = "/uploads", length: int | None = None) -> Scope:
    headers = [(b"content-length", str(length).encode())] if length is not None else []
    return {"type": "http", "method": method, "path": path, "headers": headers}


class Recorder:
    """An application that reads the whole body, and a client that sends it in chunks."""

    def __init__(self, chunks: list[int]) -> None:
        self.chunks = chunks
        self.read = 0
        self.sent: list[Message] = []

    async def receive(self) -> Message:
        size = self.chunks.pop(0) if self.chunks else 0
        return {"type": "http.request", "body": b"x" * size, "more_body": bool(self.chunks)}

    async def send(self, message: Message) -> None:
        self.sent.append(message)

    async def app(self, scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            self.read += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})


async def run(recorder: Recorder, request: Scope) -> None:
    await BodyLimit(recorder.app, path="/uploads", limit=LIMIT)(
        request, recorder.receive, recorder.send
    )


async def test_a_declared_size_over_the_limit_is_answered_without_reading_the_body() -> None:
    recorder = Recorder([10])
    await run(recorder, scope(length=LIMIT + 1))
    assert recorder.sent[0]["status"] == 413 and recorder.read == 0


async def test_an_undeclared_body_is_counted_and_stopped_as_it_arrives() -> None:
    recorder = Recorder([400, 400, 400, 400, 400])  # 2000 bytes, no Content-Length
    with pytest.raises(HTTPException) as caught:
        await run(recorder, scope())
    assert caught.value.status_code == 413
    assert recorder.read <= LIMIT + 400  # it stopped; the remaining chunks were never read


async def test_a_body_within_the_limit_goes_through() -> None:
    recorder = Recorder([400, 400])
    await run(recorder, scope(length=800))
    assert recorder.read == 800 and recorder.sent[0]["status"] == 200


async def test_a_malformed_content_length_is_refused() -> None:
    recorder = Recorder([1])
    request = scope()
    request["headers"] = [(b"content-length", b"12abc")]
    await run(recorder, request)
    assert recorder.sent[0]["status"] == 413


@pytest.mark.parametrize("other", [scope(method="GET"), scope(path="/")])
async def test_other_requests_are_not_limited(other: Scope) -> None:
    recorder = Recorder([5000])
    await run(recorder, other)
    assert recorder.read == 5000

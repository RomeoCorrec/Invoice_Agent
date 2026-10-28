"""A cap on the size of an upload, applied before the body is parsed.

Starlette writes a multipart file to a temporary file as it arrives, without a limit, so the
cap has to sit in front of the parser: it looks at ``Content-Length`` first, and counts the
bytes as they come for requests that do not declare one (chunked encoding).
"""

from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyLimit:
    def __init__(self, app: ASGIApp, *, path: str, limit: int) -> None:
        self.app = app
        self.path = path
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != self.path:
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.limit):
            # Outside the framework's error handling here, so the answer is given directly.
            await PlainTextResponse("The file is too large.", 413)(scope, receive, send)
            return
        received = 0

        async def counting() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise HTTPException(status_code=413)
            return message

        await self.app(scope, counting, send)

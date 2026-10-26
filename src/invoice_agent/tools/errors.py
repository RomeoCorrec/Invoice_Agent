"""What can go wrong when the agent calls a tool.

The split is the one retries need: a transient failure (timeout, dead connection, a server
that crashed) may succeed if repeated, a refusal never will.
"""


class ToolError(Exception):
    """Base class. Messages never contain tool arguments or results."""


class ToolTransientError(ToolError):
    """The call may succeed if repeated: timeout, transport failure, server crash."""


class ToolRefusedError(ToolError):
    """The tool understood the request and refused it. Repeating it changes nothing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code

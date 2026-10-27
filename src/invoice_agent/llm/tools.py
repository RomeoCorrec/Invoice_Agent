"""Tools a model may call during a conversation.

A tool is a name, a description, a JSON schema for its input, and a handler that runs in our
process. The handler returns text for the model; if it cannot answer it raises ``ToolFailure``
with a message that is safe to show the model. Anything else it raises is reported to the
model as a generic failure, never with the exception's text.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

MAX_TOOL_RESULT_CHARS = 6000
"""A tool result longer than this is cut: it goes back into the model's context."""


class ToolFailure(Exception):
    """The tool could not answer. The message is passed back to the model as the result."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class ToolCall:
    """One tool call the model made, for the audit trail."""

    name: str
    arguments: dict[str, Any]
    ok: bool
    error: str | None = None


def run_tool(
    tools: Mapping[str, ToolSpec], name: str, arguments: dict[str, Any]
) -> tuple[str, ToolCall]:
    """Run the tool the model asked for; return what to tell it and the record of the call."""
    spec = tools.get(name)
    if spec is None:
        return "unknown tool", ToolCall(name, arguments, ok=False, error="unknown tool")
    try:
        text = spec.handler(arguments)
    except ToolFailure as failure:
        message = str(failure)[:300]
        return message, ToolCall(name, arguments, ok=False, error=message)
    except Exception:
        return "the tool failed", ToolCall(name, arguments, ok=False, error="the tool failed")
    return text[:MAX_TOOL_RESULT_CHARS], ToolCall(name, arguments, ok=True)

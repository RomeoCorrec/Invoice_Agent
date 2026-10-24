"""Run a function in a child process with a deadline and a memory ceiling.

A pure-Python parser fed a hostile document can loop for minutes or eat memory; a thread
cannot be stopped, a process can be killed. The child is started with the ``spawn`` method
(nothing is inherited from the worker), receives its arguments by pickle, and returns its
result or a safe error message through a pipe. If it does not answer in time it is killed.
"""

import multiprocessing
import sys
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

from invoice_agent.config import IngestionLimits
from invoice_agent.ingestion.document import ExtractionError
from invoice_agent.ingestion.reading import RawDocument, read_document

DEFAULT_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024


def _limit_memory(limit: int | None) -> None:
    """Cap the child's address space where the platform allows it (Linux; not Windows)."""
    if limit is None or sys.platform == "win32":
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):  # pragma: no cover - platform dependent
        pass


def _child(
    connection: Connection,
    function: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    memory_limit: int | None,
) -> None:
    try:
        _limit_memory(memory_limit)
        connection.send(("ok", function(*args, **kwargs)))
    except ExtractionError as exc:
        connection.send(("extraction_error", str(exc)))
    except MemoryError:
        connection.send(("failure", "MemoryError"))
    except BaseException as exc:
        connection.send(("failure", type(exc).__name__))
    finally:
        connection.close()


def run_isolated(
    function: Callable[..., Any],
    *args: Any,
    timeout_s: float,
    memory_limit: int | None = DEFAULT_MEMORY_LIMIT_BYTES,
    **kwargs: Any,
) -> Any:
    """Call ``function(*args, **kwargs)`` in a child process; raise ExtractionError on failure.

    ``function`` must be importable from the top level of a module (it is pickled by name).
    """
    context = multiprocessing.get_context("spawn")
    parent_end, child_end = context.Pipe(duplex=False)
    process = context.Process(
        target=_child, args=(child_end, function, args, kwargs, memory_limit), daemon=True
    )
    process.start()
    child_end.close()
    try:
        if not parent_end.poll(timeout_s):
            raise ExtractionError(f"reading the document took longer than {timeout_s:g} seconds")
        try:
            kind, value = parent_end.recv()
        except EOFError as exc:
            raise ExtractionError("the document reader stopped unexpectedly") from exc
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=5)
        parent_end.close()
    if kind == "ok":
        return value
    if kind == "extraction_error":
        raise ExtractionError(value)
    raise ExtractionError(f"the document reader failed ({value})")


def read_document_isolated(
    content: bytes,
    limits: IngestionLimits,
    *,
    want_text: bool,
    timeout_s: float,
    memory_limit: int | None = DEFAULT_MEMORY_LIMIT_BYTES,
) -> RawDocument:
    """``read_document`` in a child process: same result, bounded in time and memory."""
    raw = run_isolated(
        read_document,
        content,
        limits,
        want_text=want_text,
        timeout_s=timeout_s,
        memory_limit=memory_limit,
    )
    assert isinstance(raw, RawDocument)
    return raw

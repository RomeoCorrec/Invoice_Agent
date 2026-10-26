"""The diagram in the README must be the graph the code builds."""

import re
from pathlib import Path
from unittest.mock import MagicMock

from langgraph.checkpoint.memory import InMemorySaver

from invoice_agent.agent.graph.build import build_graph
from invoice_agent.agent.graph.deps import AgentDeps
from invoice_agent.config import IngestionLimits

README = Path(__file__).parents[3] / "README.md"


def drawn() -> str:
    deps = AgentDeps(
        session_factory=MagicMock(),
        store=MagicMock(),
        extractor=None,
        iban_key=b"k" * 16,
        limits=IngestionLimits(),
    )
    return build_graph(deps, InMemorySaver()).get_graph().draw_mermaid().strip()


def documented() -> str:
    match = re.search(r"```mermaid\n(.*?)\n```", README.read_text(encoding="utf-8"), re.DOTALL)
    assert match, "the README has no mermaid diagram"
    return match.group(1).strip()


def test_the_readme_diagram_is_the_graph_the_code_builds() -> None:
    assert documented() == drawn(), (
        "the graph changed: replace the mermaid block of the README with:\n" + drawn()
    )

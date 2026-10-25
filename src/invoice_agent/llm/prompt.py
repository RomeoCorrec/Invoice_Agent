"""Versioned prompts, stored as files and never built from strings scattered in the code."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    text: str
    sha256: str

    @property
    def label(self) -> str:
        """Identifies the exact prompt in traces and evaluation runs."""
        return f"{self.name}.v{self.version}@{self.sha256[:8]}"


def load_prompt(name: str, version: int) -> Prompt:
    path = PROMPTS_DIR / f"{name}.v{version}.md"
    text = path.read_text(encoding="utf-8")
    return Prompt(
        name=name,
        version=version,
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )

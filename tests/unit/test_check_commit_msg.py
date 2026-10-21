import pytest

from scripts.check_commit_msg import check


@pytest.mark.parametrize(
    "message",
    [
        "feat(ingestion): parse Factur-X CII lines",
        "fix: handle empty invoice",
        "refactor(db)!: rename tables",
        "docs: add ADR\n\nBody explaining why.",
        "Merge pull request #1 from RomeoCorrec/feat/00-scaffolding",
        "feat: add thing\n# comment lines are ignored",
    ],
)
def test_valid_messages(message: str) -> None:
    assert check(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "",
        "update stuff",
        "Feat: capitalised type",
        "feat(Scope): uppercase scope",
        "feat:no space",
        "wip: unknown type",
        "feat: " + "x" * 80,
        "feat: ok\nbody without blank line",
    ],
)
def test_invalid_messages(message: str) -> None:
    assert check(message) is not None

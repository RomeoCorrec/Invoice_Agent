from itertools import product

import pytest

from invoice_agent.domain.invoice_status import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidStatusTransition,
    InvoiceStatus,
    can_transition,
    check_transition,
)

S = InvoiceStatus

# Written out by hand on purpose: deriving it from the table would make the test circular.
EXPECTED_ALLOWED = {
    (S.RECEIVED, S.EXTRACTED),
    (S.RECEIVED, S.FAILED),
    (S.EXTRACTED, S.VALIDATED),
    (S.EXTRACTED, S.NEEDS_REVIEW),
    (S.EXTRACTED, S.FAILED),
    (S.VALIDATED, S.APPROVED),
    (S.VALIDATED, S.REJECTED),
    (S.VALIDATED, S.FAILED),
    (S.NEEDS_REVIEW, S.APPROVED),
    (S.NEEDS_REVIEW, S.REJECTED),
    (S.NEEDS_REVIEW, S.FAILED),
    (S.APPROVED, S.POSTED),
    (S.APPROVED, S.FAILED),
    (S.FAILED, S.RECEIVED),
}
ALL_PAIRS = list(product(S, S))
FORBIDDEN = [pair for pair in ALL_PAIRS if pair not in EXPECTED_ALLOWED]


def test_every_status_has_an_entry_in_the_table() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_table_matches_the_expected_transitions() -> None:
    actual = {(src, dst) for src, targets in ALLOWED_TRANSITIONS.items() for dst in targets}
    assert actual == EXPECTED_ALLOWED


def test_forbidden_transitions_cover_the_rest_of_the_matrix() -> None:
    assert len(FORBIDDEN) + len(EXPECTED_ALLOWED) == len(S) ** 2


@pytest.mark.parametrize(("current", "target"), sorted(EXPECTED_ALLOWED))
def test_allowed_transition(current: InvoiceStatus, target: InvoiceStatus) -> None:
    assert can_transition(current, target)
    check_transition(current, target)


@pytest.mark.parametrize(("current", "target"), FORBIDDEN)
def test_forbidden_transition_raises(current: InvoiceStatus, target: InvoiceStatus) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidStatusTransition) as error:
        check_transition(current, target)
    assert (error.value.current, error.value.target) == (current, target)


def test_terminal_statuses() -> None:
    assert {S.POSTED, S.REJECTED} == TERMINAL_STATUSES


def test_nothing_reaches_posted_without_approval() -> None:
    sources = {src for src, dst in EXPECTED_ALLOWED if dst is S.POSTED}
    assert sources == {S.APPROVED}

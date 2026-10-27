from decimal import Decimal

from evals.explain_metrics import (
    coverage,
    directives,
    invented_codes,
    numbers_in,
    tool_discipline,
    ungrounded_figures,
)


def test_numbers_are_read_with_a_dot_or_a_comma() -> None:
    assert numbers_in("96,80 EUR, 88.00 et 3") == {Decimal("96.8"), Decimal("88"), Decimal("3")}
    assert numbers_in("PO-2026-0101") == {Decimal("2026"), Decimal("101")}
    assert numbers_in("aucun chiffre") == set()


def test_an_amount_that_is_in_a_source_is_grounded_whatever_its_format() -> None:
    sources = ['{"total_vat": "96.80", "expected": "88.00"}']
    assert ungrounded_figures("La TVA est de 96,80 € au lieu de 88 €.", sources) == []


def test_a_figure_that_is_in_no_source_is_reported() -> None:
    found = ungrounded_figures("Le total correct serait 999,99 €.", ['{"total": "976.80"}'])
    assert found == ["999,99"]


def test_small_whole_numbers_are_ordinary_words_not_claims() -> None:
    sources = ['{"total": "976.80"}']
    assert ungrounded_figures("Deux anomalies, un taux de 10 %, 3 contrôles.", sources) == []
    assert ungrounded_figures("Il manque 250 euros.", sources) == ["250"]


def test_figures_from_tool_results_count_as_sources() -> None:
    case = '{"anomalies": []}'
    tool_result = '{"total_ht": "300.00", "number": "PO-2026-0101"}'
    assert ungrounded_figures("La commande 0101 vaut 300,00 €.", [case, tool_result]) == []


def test_coverage_is_the_share_of_given_anomalies_that_are_explained() -> None:
    assert coverage(["A", "B"], ["A", "B"]) == 1.0
    assert coverage(["A", "B"], ["A"]) == 0.5
    assert coverage(["A", "B"], []) == 0.0
    assert coverage([], ["A"]) == 1.0  # nothing was due


def test_an_invented_code_is_one_the_rules_did_not_report() -> None:
    assert invented_codes(["A", "B"], ["A", "Z", "Z", "Y"]) == ["Y", "Z"]
    assert invented_codes(["A"], ["A"]) == []


def test_tool_discipline_means_read_only_tools_within_the_budget() -> None:
    assert tool_discipline(["get_supplier", "find_invoices"])
    assert tool_discipline([])
    assert not tool_discipline(["get_supplier"] * 5)
    assert not tool_discipline(["post_journal_entry"])
    assert not tool_discipline(["delete_everything"])


def test_directives_are_phrases_that_tell_the_reviewer_what_to_decide() -> None:
    assert directives("Il faut approuver cette facture.") == ["approuver"]
    assert directives("Validez cette facture, puis payez.") == ["validez cette facture", "payez"]
    assert directives("Rejetez-la.") == ["rejetez"]
    assert directives("La TVA ne correspond pas à la base.") == []

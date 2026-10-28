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


# --- sentences the model really wrote (claude-haiku-5-5, 2026-10-28) --------------------------

INV_16_CASE = (
    '{"invoice_total_ht": "4750.00", "remaining_ht": "3800.00", "order_total_ht": "3800.00",'
    ' "already_billed_ht": "0.00", "total_ttc": "5700.00", "total_vat": "950.00"}'
)


def test_a_french_thousands_separator_is_part_of_the_number() -> None:
    nbsp, narrow = "\u00a0", "\u202f"
    text = f"5 700,00 EUR et 4{nbsp}750,00 et 3{narrow}800,00"
    assert numbers_in(text) == {Decimal("5700"), Decimal("4750"), Decimal("3800")}
    assert numbers_in("SIREN 963 203 419") == {Decimal("963203419")}
    assert numbers_in("2 anomalies, 10 %") == {Decimal("2"), Decimal("10")}


def test_the_real_inv_16_explanation_is_grounded() -> None:
    text = (
        "Facture de 5 700,00 EUR TTC (4 750,00 EUR HT, TVA 20 % de 950,00 EUR). Le montant HT "
        "(4 750,00 EUR) est supérieur au montant restant (3 800,00 EUR HT)."
    )
    assert ungrounded_figures(text, [INV_16_CASE]) == []


def test_a_gap_between_two_amounts_of_the_case_is_arithmetic_not_an_invention() -> None:
    case = '{"declared_vat": "96.80", "expected_vat": "88.00", "total_ttc": "976.80"}'
    assert (
        ungrounded_figures("La TVA est de 96,80 € au lieu de 88,00 €, soit 8,80 € de trop.", [case])
        == []
    )
    case = '{"message": "total TTC 390.00 differs from HT + VAT 360.00", "ht": "300.00"}'
    assert ungrounded_figures("390,00 € indiqué contre 360,00 € (écart de 30,00 €).", [case]) == []


def test_a_figure_that_is_neither_in_the_case_nor_a_gap_or_a_sum_is_still_caught() -> None:
    case = '{"declared_vat": "96.80", "expected_vat": "88.00"}'
    assert ungrounded_figures("La TVA correcte serait de 91,23 €.", [case]) == ["91,23"]
    assert ungrounded_figures("Le montant réel est 12 345,67 €.", [case]) == ["12 345,67"]

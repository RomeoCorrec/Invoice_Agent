"""VAT and totals arithmetic. Amounts are compared to the cent, without tolerance."""

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from pydantic import JsonValue

from invoice_agent.domain.invoice import InvoiceData
from invoice_agent.domain.rules.anomaly import Anomaly, AnomalyCode

CENT = Decimal("0.01")


def expected_vat(basis: Decimal, rate: Decimal) -> Decimal:
    """VAT on ``basis`` at ``rate`` percent, rounded half up to the cent."""
    return (basis * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def _sum_by_rate(pairs: list[tuple[Decimal, Decimal]]) -> dict[Decimal, Decimal]:
    totals: dict[Decimal, Decimal] = defaultdict(Decimal)
    for rate, amount in pairs:
        totals[rate] += amount
    return dict(totals)


def check_vat(data: InvoiceData) -> list[Anomaly]:
    """Each declared VAT must equal declared basis x rate; each used rate must be declared."""
    declared = _sum_by_rate([(line.rate, line.vat) for line in data.vat_breakdown])
    declared_basis = _sum_by_rate([(line.rate, line.basis) for line in data.vat_breakdown])
    problems: list[str] = []
    evidence: dict[str, JsonValue] = {}
    for rate in sorted({line.vat_rate for line in data.lines} | set(declared)):
        if rate not in declared:
            problems.append(f"rate {rate}% is used by lines but not in the VAT breakdown")
            continue
        expected = expected_vat(declared_basis[rate], rate)
        if declared[rate] != expected:
            problems.append(f"rate {rate}%: VAT is {declared[rate]}, expected {expected}")
            evidence[f"vat_{rate}"] = f"billed {declared[rate]}, expected {expected}"
    if not problems:
        return []
    return [
        Anomaly(
            code=AnomalyCode.VAT_AMOUNT_MISMATCH,
            message="; ".join(problems),
            evidence=evidence,
        )
    ]


def check_totals(data: InvoiceData) -> list[Anomaly]:
    """Lines, VAT breakdown and the three totals must be consistent with each other."""
    problems: list[str] = []
    lines_ht = sum((line.amount_ht for line in data.lines), start=Decimal("0.00"))
    if data.total_ht != lines_ht:
        problems.append(f"total HT {data.total_ht} differs from the sum of lines {lines_ht}")
    breakdown_vat = sum((line.vat for line in data.vat_breakdown), start=Decimal("0.00"))
    if data.total_vat != breakdown_vat:
        problems.append(f"total VAT {data.total_vat} differs from the breakdown {breakdown_vat}")
    if data.total_ttc != data.total_ht + data.total_vat:
        problems.append(
            f"total TTC {data.total_ttc} differs from HT + VAT {data.total_ht + data.total_vat}"
        )
    lines_by_rate = _sum_by_rate([(line.vat_rate, line.amount_ht) for line in data.lines])
    basis_by_rate = _sum_by_rate([(line.rate, line.basis) for line in data.vat_breakdown])
    for rate in sorted(set(lines_by_rate) | set(basis_by_rate)):
        in_lines = lines_by_rate.get(rate, Decimal(0))
        in_breakdown = basis_by_rate.get(rate, Decimal(0))
        if in_lines != in_breakdown:
            problems.append(
                f"rate {rate}%: lines add up to {in_lines}, VAT basis is {in_breakdown}"
            )
    if not problems:
        return []
    return [Anomaly(code=AnomalyCode.TOTAL_MISMATCH, message="; ".join(problems))]

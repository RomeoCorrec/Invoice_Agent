"""What a draft has to pass before a person sees it, written as code.

The same checks apply to a template and to a model's text: nothing in a message that leaves
the company depends on trusting the author of the draft. A draft that fails is not shown; the
caller falls back to the template, which a test proves passes for every code.

The rules (each exists because the alternative is a real incident):

* it cites the invoice, so the supplier knows what it is about;
* it asks about every anomaly it was given, once, and about no other;
* every figure comes from the facts, so a model cannot invent an amount or a date;
* no IBAN, no address, no link: a message is not a channel to move money or to lead somewhere;
* no promise of payment or acceptance, which only a person who approved can make;
* it never asks the supplier for bank details, the classic fraud.
"""

import re

from invoice_agent.agent.correspondence.facts import QueryFacts, digit_groups
from invoice_agent.agent.correspondence.templates import QueryDraft
from invoice_agent.domain.iban import contains_iban, fold_text

NUMBER = re.compile(r"\d+(?:[ ,." + chr(0xA0) + r"]\d+)*")
"""A number as written in French or English: ``2 640,00``, ``2640.00``, ``0457``."""
SHORT = 2
"""A number of up to two digits is a list number or a count, not an amount."""
LINK_OR_ADDRESS = re.compile(r"https?://|www\.|\S+@\S+", re.IGNORECASE)
PROMISE = re.compile(
    r"nous\s+(?:allons|vous\s+(?:allons|rembourserons|paierons))\s+(?:vous\s+)?(?:r[eé]gler|payer|rembourser)"
    r"|nous\s+(?:r[eè]glerons|paierons|rembourserons|procéderons\s+au\s+(?:paiement|r[eè]glement|virement))"
    r"|(?:paiement|r[eè]glement|virement)\s+(?:sera|va\s+être|est\s+programmé)"
    r"|(?:facture|document)\s+(?:est\s+|a\s+été\s+|sera\s+)?(?:valid[eé]e?|approuv[eé]e?|accept[eé]e?|pay[eé]e?)"
    r"|nous\s+(?:validons|approuvons|acceptons)\s+(?:votre|cette)",
    re.IGNORECASE,
)
ASKS_FOR_BANK_DETAILS = re.compile(
    r"(?:envoy|communiqu|transmet|donn|indiqu|fournir|adress)\w*[^.\n]{0,60}"
    r"(?:\biban\b|\brib\b|coordonn[ée]es\s+bancaires|compte\s+bancaire)",
    re.IGNORECASE,
)
MIN_BODY, MAX_BODY, MAX_SUBJECT = 40, 3000, 200


def allowed_numbers(facts: QueryFacts) -> set[str]:
    """The numbers a draft may contain, as bare digits (``2 640,00`` is ``264000``)."""
    allowed = digit_groups(facts.invoice_number) | digit_groups(facts.purchase_order_ref)
    allowed.add(re.sub(r"\D", "", facts.total_ttc))
    for anomaly in facts.anomalies:
        # Our own description of the finding quotes the amounts and the rate it is about.
        allowed.update(re.sub(r"\D", "", token) for token in NUMBER.findall(anomaly.message))
    return allowed


def check_draft(draft: QueryDraft, facts: QueryFacts) -> list[str]:
    """The problems of a draft, empty when it can be shown. Each is a short stable sentence."""
    problems: list[str] = []
    text = fold_text(f"{draft.subject}\n{draft.body}")

    if "\n" in draft.subject or "\r" in draft.subject or len(draft.subject) > MAX_SUBJECT:
        problems.append("the subject must be one short line")
    if not MIN_BODY <= len(draft.body) <= MAX_BODY:
        problems.append("the body has an unreasonable length")

    if facts.invoice_number and facts.invoice_number not in text:
        problems.append("the invoice number is not cited")

    if len(set(draft.addressed)) != len(draft.addressed):
        problems.append("an anomaly is addressed twice")
    if set(draft.addressed) - set(facts.codes):
        problems.append("it addresses an anomaly that was not given")
    if set(facts.codes) - set(draft.addressed):
        problems.append("it leaves an anomaly unaddressed")

    allowed = allowed_numbers(facts)
    for token in NUMBER.findall(text):
        digits = re.sub(r"\D", "", token)
        if len(digits) > SHORT and digits not in allowed:
            problems.append("a figure does not come from the facts")
            break

    if contains_iban(text):
        problems.append("it contains bank details")
    if LINK_OR_ADDRESS.search(text):
        problems.append("it contains a link or an address")
    if PROMISE.search(text):
        problems.append("it promises payment or acceptance")
    if ASKS_FOR_BANK_DETAILS.search(text):
        problems.append("it asks for bank details")
    return problems

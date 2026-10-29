"""The message to the supplier, written in plain Python: the baseline and the fallback.

One request per anomaly code, in French. The wording is chosen with the same care as the rest of
the project: it never promises payment, never says the invoice is accepted, never asks for bank
details (a change of bank details is handled by a call to a known number, not by an answer to a
message), and never repeats a sentence of the document. A code the templates do not know gets a
generic request, so a new rule cannot make the message fail.
"""

from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field

from invoice_agent.agent.correspondence.facts import QueryFacts

GENERIC_REQUEST = "Un point de cette facture appelle une vérification de notre part."


class QueryDraft(BaseModel):
    """A message to the supplier: what a template or a model produces, before a person reads it."""

    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    addressed: tuple[str, ...] = Field(max_length=20)
    """The anomaly codes the message asks about, each once."""


REQUESTS: dict[str, str] = {
    "VAT_AMOUNT_MISMATCH": (
        "Le montant de TVA indiqué ne correspond pas à la base et au taux de TVA appliqués. "
        "Merci de vérifier et de nous renvoyer une facture rectifiée."
    ),
    "TOTAL_MISMATCH": (
        "Le total TTC ne correspond pas à la somme du total hors taxes et de la TVA. "
        "Merci de vérifier et de nous renvoyer une facture rectifiée."
    ),
    "DUPLICATE_INVOICE": (
        "Il semble que nous ayons déjà reçu cette facture. Merci de nous indiquer s'il s'agit "
        "d'un doublon ou d'un document différent."
    ),
    "UNKNOWN_SUPPLIER": (
        "Nous ne parvenons pas à rattacher votre société à nos fournisseurs référencés. "
        "Merci de nous indiquer votre interlocuteur habituel dans notre société."
    ),
    "INVALID_SIREN": (
        "Le numéro SIREN indiqué sur la facture ne nous semble pas valide. "
        "Merci de vérifier vos mentions légales et de nous renvoyer une facture corrigée."
    ),
    "IBAN_MISMATCH": (
        "Les coordonnées bancaires figurant sur la facture ne correspondent pas à celles que "
        "nous avons enregistrées. Par sécurité, nous ne modifions jamais des coordonnées "
        "bancaires à la suite d'un message : si elles ont changé, merci de contacter votre "
        "interlocuteur habituel par téléphone."
    ),
    "PO_NOT_FOUND": (
        "La commande citée sur la facture n'existe pas dans notre système. "
        "Merci de nous indiquer la référence exacte de notre bon de commande."
    ),
    "NO_PO_MATCH": (
        "La facture ne cite aucune commande que nous puissions rapprocher de nos achats. "
        "Merci de nous indiquer la référence de notre bon de commande."
    ),
    "AMOUNT_EXCEEDS_PO": (
        "Le montant facturé dépasse celui de notre commande. "
        "Merci de justifier l'écart ou de nous renvoyer une facture conforme à la commande."
    ),
    "PO_ALREADY_INVOICED": (
        "La commande concernée a déjà été facturée. "
        "Merci de nous indiquer à quoi correspond cette nouvelle facture."
    ),
    "PROMPT_INJECTION_SUSPECTED": (
        "La facture comporte des mentions inhabituelles que nous ne pouvons pas traiter "
        "automatiquement. Merci de nous renvoyer la facture sans ces mentions."
    ),
}


def amount_in_french(total: str, currency: str) -> str:
    """``2640.00`` and ``EUR`` give ``2 640,00 EUR``. An unreadable amount gives an empty text."""
    try:
        value = Decimal(total)
    except InvalidOperation:
        return ""
    whole, _, cents = f"{value:.2f}".partition(".")
    groups = f"{int(whole):,}".replace(",", " ")
    return f"{groups},{cents} {currency}"


def template_draft(facts: QueryFacts) -> QueryDraft:
    """The message a template writes for these facts. Deterministic: same facts, same text."""
    label = f"Facture {facts.invoice_number}" if facts.invoice_number else "Votre facture"
    prefix = "Relance - " if facts.reminder else ""
    subject = f"{prefix}{label} : demande de correction"

    reference = f"votre facture {facts.invoice_number}" if facts.invoice_number else "votre facture"
    amount = amount_in_french(facts.total_ttc, facts.currency)
    what = f"{reference} d'un montant de {amount} TTC" if amount else reference
    opening = (
        f"Nous revenons vers vous au sujet de {what}, restée sans réponse de votre part."
        if facts.reminder
        else f"Nous avons bien reçu {what}, mais nous ne pouvons pas la traiter en l'état."
    )
    requests = [REQUESTS.get(code, GENERIC_REQUEST) for code in facts.codes] or [GENERIC_REQUEST]
    numbered = "\n".join(f"{n}. {text}" for n, text in enumerate(requests, start=1))
    body = (
        "Bonjour,\n\n"
        f"{opening}\n\n"
        f"{numbered}\n\n"
        "Vous pouvez répondre directement à ce message, avec la facture corrigée en pièce "
        "jointe s'il y a lieu.\n\n"
        "Cordialement,\n"
        "Le service comptabilité fournisseurs"
    )
    return QueryDraft(subject=subject, body=body, addressed=facts.codes)

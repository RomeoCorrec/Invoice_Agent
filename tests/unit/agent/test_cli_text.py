from invoice_agent.presentation import safe_text

REPLACEMENT = "\N{REPLACEMENT CHARACTER}"


def test_ordinary_text_is_unchanged() -> None:
    assert safe_text("Papeterie Mercier SARL - n° 12/É") == "Papeterie Mercier SARL - n° 12/É"


def test_escape_sequences_newlines_and_bidi_overrides_are_replaced() -> None:
    hostile = "a\x1b[2Jb\rc\nd‮e\x00f"
    cleaned = safe_text(hostile)
    assert cleaned == f"a{REPLACEMENT}[2Jb{REPLACEMENT}c{REPLACEMENT}d{REPLACEMENT}e{REPLACEMENT}f"
    assert all(ch.isprintable() for ch in cleaned)


def test_the_length_is_bounded() -> None:
    assert len(safe_text("x" * 1000, 20)) == 20


def test_a_hostile_entry_label_cannot_drive_the_terminal() -> None:
    from invoice_agent.agent.__main__ import render_request

    request = {
        "invoice_id": "id",
        "status": "validated",
        "invoice_number": "A\x1b[2J",
        "seller": "Evil\rCorp‮",
        "currency": "EUR",
        "total_ht": "1.00",
        "total_vat": "0.20",
        "total_ttc": "1.20",
        "anomaly_codes": [],
        "entry_blocked": None,
        "entry": {
            "journal": "ACH",
            "entry_date": "2026-10-01",
            "lines": [
                {
                    "account": "6064",
                    "side": "debit",
                    "amount": "1.00",
                    "label": "x\x1b]0;pwned\x07",
                },
                {"account": "401X", "side": "credit", "amount": "1.00", "label": "y\nFAKE LINE"},
            ],
        },
    }
    out = render_request(request)
    assert "\x1b" not in out and "\r" not in out and "‮" not in out and "\x07" not in out
    assert "FAKE LINE" in out and out.count("\n") == len(out.splitlines()) - 1
    assert len(out.splitlines()) == 8  # the newline in a label did not add a line


def test_a_blocked_entry_is_explained() -> None:
    from invoice_agent.agent.__main__ import render_request

    out = render_request(
        {
            "invoice_id": "id",
            "status": "needs_review",
            "invoice_number": "N",
            "seller": "S",
            "currency": "EUR",
            "total_ht": "1.00",
            "total_vat": "0.20",
            "total_ttc": "1.20",
            "anomaly_codes": ["UNKNOWN_SUPPLIER"],
            "entry": None,
            "entry_blocked": {"reason": "unknown_supplier", "detail": "no known supplier"},
        }
    )
    assert "entry     none: unknown_supplier" in out and "can only be rejected" in out


def test_the_model_s_explanation_is_shown_labelled_and_cleaned() -> None:
    from invoice_agent.agent.__main__ import render_request

    out = render_request(
        {
            "invoice_id": "id",
            "status": "needs_review",
            "invoice_number": "N",
            "seller": "S",
            "currency": "EUR",
            "total_ht": "1.00",
            "total_vat": "0.20",
            "total_ttc": "1.20",
            "anomaly_codes": ["VAT_AMOUNT_MISMATCH"],
            "entry": None,
            "entry_blocked": None,
            "explanation": {
                "summary": "Look at the VAT.\x1b[2J",
                "items": [{"code": "VAT_AMOUNT_MISMATCH", "explanation": "x\rFAKE\nLINE"}],
            },
        }
    )
    assert "written by the model" in out
    assert "VAT_AMOUNT_MISMATCH: x" in out
    assert "\x1b" not in out and "\r" not in out
    assert not any(line.strip() == "LINE" for line in out.splitlines())

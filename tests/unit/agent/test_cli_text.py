from invoice_agent.agent.__main__ import safe_text

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

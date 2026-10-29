from invoice_agent.presentation import safe_block


def test_line_breaks_of_a_message_are_kept() -> None:
    assert safe_block("Bonjour,\r\nmerci.\rCordialement") == "Bonjour,\nmerci.\nCordialement"


def test_other_control_and_invisible_characters_are_replaced() -> None:
    shown = safe_block("a\x1b[31mb‮c​d")
    assert "\x1b" not in shown and "‮" not in shown and "​" not in shown
    assert shown.startswith("a") and shown.endswith("d")


def test_a_long_message_is_cut() -> None:
    assert len(safe_block("x" * 10_000, 100)) == 100

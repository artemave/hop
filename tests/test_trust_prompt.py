from __future__ import annotations

from typing import Callable

from hop.trust_prompt import ask


def _inputs(*lines: str) -> Callable[[str], str]:
    values = iter(lines)

    def read(_prompt: str) -> str:
        return next(values)

    return read


def test_trust_key_returns_true() -> None:
    assert ask("/proj/.hop.toml", "content", prompt_input=_inputs("t"), output=lambda _line: None) is True


def test_show_key_prints_content_then_reprompts() -> None:
    printed: list[str] = []

    result = ask(
        "/proj/.hop.toml",
        "activate = true",
        prompt_input=_inputs("s", "t"),
        output=printed.append,
    )

    assert result is True
    assert "activate = true" in printed


def test_unrecognized_input_reprompts_without_trusting_or_aborting() -> None:
    printed: list[str] = []

    result = ask(
        "/proj/.hop.toml",
        "content",
        prompt_input=_inputs("nonsense", "t"),
        output=printed.append,
    )

    assert result is True
    assert any("unrecognized input" in line for line in printed)


def test_eof_aborts() -> None:
    def read(_prompt: str) -> str:
        raise EOFError

    assert ask("/proj/.hop.toml", "content", prompt_input=read, output=lambda _line: None) is False


def test_keyboard_interrupt_aborts() -> None:
    def read(_prompt: str) -> str:
        raise KeyboardInterrupt

    assert ask("/proj/.hop.toml", "content", prompt_input=read, output=lambda _line: None) is False


def test_input_is_stripped() -> None:
    assert ask("/proj/.hop.toml", "content", prompt_input=_inputs("  t  "), output=lambda _line: None) is True

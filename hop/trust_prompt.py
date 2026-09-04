from __future__ import annotations

from typing import Callable

PromptInput = Callable[[str], str]
PromptOutput = Callable[[str], object]

_BANNER = (
    "It can run shell commands (backend activate/prepare/teardown, port translation,\n"
    "window commands, editor keystrokes)."
)
_MENU = "  [t] trust it and continue\n  [s] show it\n  Ctrl-C / Ctrl-D to abort (no session created)"


def ask(
    config_path: str,
    content: str,
    *,
    prompt_input: PromptInput = input,
    output: PromptOutput = print,
) -> bool:
    output(f"hop: {config_path} is not trusted.")
    output(_BANNER)
    while True:
        output(_MENU)
        try:
            line = prompt_input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            output("")
            return False
        if line == "t":
            return True
        if line == "s":
            output(content)
            continue
        output(f"hop: unrecognized input {line!r} — press t, s, or Ctrl-C/Ctrl-D to abort.")

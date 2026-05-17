from __future__ import annotations


class HopError(RuntimeError):
    """Base error for hop command execution."""

    def __init__(self, *args: object, surfaced_by_popup: bool = False) -> None:
        super().__init__(*args)
        # When True, the user has already seen this failure inside a kitten-panel
        # popup (e.g. a lifecycle popup's held-open shell). `cli.main`'s catch-all
        # error popup checks this flag to avoid double-surfacing the same error.
        self.surfaced_by_popup = surfaced_by_popup


class IntegrationNotImplementedError(HopError):
    """Raised when a command reaches an integration scaffold that is not wired yet."""

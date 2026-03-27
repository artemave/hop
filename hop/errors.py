from __future__ import annotations


class HopError(RuntimeError):
    """Base error for hop command execution."""


class IntegrationNotImplementedError(HopError):
    """Raised when a command reaches an integration scaffold that is not wired yet."""

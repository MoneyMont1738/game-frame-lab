"""Helper for features that are scaffolded now and implemented in a later phase."""

from __future__ import annotations


class NotYetImplemented(NotImplementedError):
    pass


def pending(phase: int, feature: str) -> NotYetImplemented:
    return NotYetImplemented(f"{feature} is planned for Phase {phase} and is not implemented yet.")

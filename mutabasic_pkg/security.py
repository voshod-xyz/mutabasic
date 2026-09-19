"""Explicit security controls for host-facing operations.

This is a policy layer, not a sandbox.  File access remains governed by the
OS account running the interpreter.
"""
from dataclasses import dataclass, field
import shlex
from pathlib import Path

from .core_types import BasicPolicyError


@dataclass(frozen=True)
class ShellPolicy:
    """Shell permission and optional executable allow-list.

    ``enabled`` is deliberately opt-in.  An empty ``commands`` set means
    unrestricted commands after opt-in (legacy ``--allow-shell`` behavior).
    """

    enabled: bool = False
    commands: frozenset[str] = field(default_factory=frozenset)

    def check(self, command: str) -> None:
        if not self.enabled:
            raise BasicPolicyError("SHELL отключён. Разрешите shell явно")
        if not self.commands:
            return
        try:
            executable = Path(shlex.split(command, posix=False)[0]).name.lower()
        except (ValueError, IndexError):
            raise BasicPolicyError("Некорректная команда SHELL")
        allowed = {Path(item).name.lower() for item in self.commands}
        if executable not in allowed:
            raise BasicPolicyError(f"Команда SHELL запрещена политикой: {executable}")

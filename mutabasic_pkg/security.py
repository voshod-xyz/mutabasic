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

    @staticmethod
    def _has_shell_syntax(command: str) -> bool:
        quoted = None
        escaped = False
        for index, char in enumerate(command):
            if escaped:
                escaped = False
                continue
            if char == "\\" and quoted != "'":
                escaped = True
                continue
            if char in ("'", '"'):
                quoted = None if quoted == char else char if quoted is None else quoted
                continue
            if char == "`" or command[index:index + 2] == "$(":
                return True
            if quoted is None and char in "&|;<>\n\r":
                return True
            if quoted is None and char == "^":
                return True
        return False

    def check(self, command: str) -> None:
        if not self.enabled:
            raise BasicPolicyError("SHELL отключён. Разрешите shell явно")
        if not self.commands:
            return
        if self._has_shell_syntax(command):
            raise BasicPolicyError(
                "Команда SHELL содержит недопустимый синтаксис оболочки"
            )
        try:
            executable = Path(shlex.split(command, posix=True)[0]).name.lower()
        except (ValueError, IndexError):
            raise BasicPolicyError("Некорректная команда SHELL")
        allowed = {Path(item).name.lower() for item in self.commands}
        if executable not in allowed:
            raise BasicPolicyError(f"Команда SHELL запрещена политикой: {executable}")

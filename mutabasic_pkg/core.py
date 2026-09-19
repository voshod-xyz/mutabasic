#!/usr/bin/env python3
"""
MutaBasic — console BASIC with transactional source-code mutation.

Requires Python 3.10+.
No third-party dependencies.

Compatibility:
    A substantial, explicitly bounded QBasic-like subset.
    Not an exact emulator of Microsoft QBasic numeric types or its runtime.

Security:
    BASIC code can access files with the current user's permissions.
    Shell execution requires --allow-shell.
    This interpreter is not a sandbox.

Persistence:
    JSON only; pickle and Python eval are not used.
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import copy
import csv
import datetime as dt
import fnmatch
import io
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .registry import FUNCTIONS, COMMANDS
from .security import ShellPolicy


NAME = "MutaBasic"
VERSION = "1.0.0"
FORMAT_VERSION = 1

IDENT = r"[A-Za-z_][A-Za-z0-9_]*[$%!#&]?"
ADDRESS_END = (sys.maxsize, sys.maxsize)

Value = int | float | str
Address = tuple[int, int]


class BasicError(Exception):
    """An error intended to be shown without a Python traceback."""


def canonical(name: str) -> str:
    return name.upper()


def display(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def split_outside(text: str, separator: str = ",") -> list[str]:
    """Split outside strings and parentheses."""
    result = []
    quoted = False
    depth = 0
    start = 0
    i = 0

    while i < len(text):
        char = text[i]

        if char == '"':
            if quoted and i + 1 < len(text) and text[i + 1] == '"':
                i += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    raise BasicError("Лишняя закрывающая скобка")
            elif char == separator and depth == 0:
                result.append(text[start:i].strip())
                start = i + 1

        i += 1

    if quoted:
        raise BasicError("Незакрытая строка")
    if depth:
        raise BasicError("Несогласованные скобки")

    result.append(text[start:].strip())
    return result


def keyword(text: str, word: str) -> int:
    """Find a standalone keyword outside strings and parentheses."""
    quoted = False
    depth = 0
    i = 0
    upper = text.upper()

    while i < len(text):
        char = text[i]

        if char == '"':
            if quoted and i + 1 < len(text) and text[i + 1] == '"':
                i += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1

            if depth == 0 and upper.startswith(word, i):
                end = i + len(word)
                left_ok = i == 0 or not (
                    text[i - 1].isalnum() or text[i - 1] in "_$%!#&"
                )
                right_ok = end == len(text) or not (
                    text[end].isalnum() or text[end] in "_$%!#&"
                )
                if left_ok and right_ok:
                    return i

        i += 1

    return -1


def strip_comment(text: str) -> str:
    quoted = False
    i = 0

    while i < len(text):
        if text[i] == '"':
            if quoted and i + 1 < len(text) and text[i + 1] == '"':
                i += 2
                continue
            quoted = not quoted
        elif text[i] == "'" and not quoted:
            return text[:i].rstrip()
        i += 1

    return text.rstrip()


def split_statements(text: str) -> list[str]:
    """
    Single-line IF and REM own the remainder of their physical source line.
    Nested single-line IF is deliberately not supported.
    """
    text = strip_comment(text)
    parts = split_outside(text, ":")
    result = []

    for i, part in enumerate(parts):
        upper = part.upper()

        if upper == "REM" or upper.startswith("REM "):
            break

        if upper.startswith("IF "):
            result.append(":".join(parts[i:]))
            break

        if part:
            result.append(part)

    return result


def parse_source(source: str) -> dict[int, str]:
    """Parse a numbered BASIC listing into a source mapping."""
    result = {}
    for line in source.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*(\d+)\s*(.*)", line)
        if not match:
            raise BasicError(f"Ожидалась нумерованная строка: {line}")
        number = int(match.group(1))
        if not 1 <= number < sys.maxsize:
            raise BasicError(f"Номер строки должен быть положительным: {number}")
        if number in result:
            raise BasicError(f"Повторный номер строки {number}")
        result[number] = match.group(2)
    return result


def literal_string(text: str) -> str:
    if not re.fullmatch(r'"(?:[^"]|"")*"', text):
        raise BasicError("Некорректная строковая константа")
    return text[1:-1].replace('""', '"')


def numeric_literal(text: str) -> int | float:
    text = text.strip()
    match = re.fullmatch(
        r"([+-]?)(?:&([HhOo])([0-9A-Fa-f]+)|"
        r"((?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?))",
        text,
    )

    if not match:
        raise BasicError(f"Не число: {text!r}")

    sign, radix, digits, decimal = match.groups()
    multiplier = -1 if sign == "-" else 1

    if radix:
        try:
            return multiplier * int(digits, 16 if radix.upper() == "H" else 8)
        except ValueError as exc:
            raise BasicError(f"Не число: {text!r}") from exc

    value = float(decimal.upper().replace("D", "E"))
    value *= multiplier

    if not math.isfinite(value):
        raise BasicError("Число вне допустимого диапазона")

    return int(value) if value.is_integer() else value


def atomic_json(path: str | Path, payload: dict) -> None:
    """Atomically replace a JSON file in the same directory."""
    path = Path(path)
    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_json(path: str | Path, kind: str) -> dict:
    obj = json.loads(Path(path).read_text(encoding="utf-8"))

    if not isinstance(obj, dict):
        raise BasicError("Ожидался JSON-объект")

    if obj.get("format") != kind or obj.get("version") != FORMAT_VERSION:
        raise BasicError(f"Неверный формат или версия: {path}")

    return obj


TOKEN = re.compile(
    r'\s*(?:'
    r'(?P<string>"(?:[^"]|"")*")'
    r'|(?P<number>&[Hh][0-9A-Fa-f]+|&[Oo][0-7]+|'
    r'(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?)'
    r'|(?P<name>' + IDENT + r')'
    r'|(?P<op><=|>=|<>|[+\-*/\\^=<>(),])'
    r')'
)


def tokenize(text: str) -> list[tuple[str, str]]:
    tokens = []
    position = 0

    while position < len(text):
        if not text[position:].strip():
            break

        match = TOKEN.match(text, position)

        if not match:
            raise BasicError(f"Недопустимая часть выражения: {text[position:]!r}")

        tokens.append((match.lastgroup, match.group(match.lastgroup)))
        position = match.end()

    tokens.append(("end", ""))
    return tokens


class Expression:
    OPERATORS = {
        "IMP": (1, 2),
        "EQV": (2, 3),
        "XOR": (3, 4),
        "OR": (4, 5),
        "AND": (5, 6),
        "=": (7, 8),
        "<>": (7, 8),
        "<": (7, 8),
        ">": (7, 8),
        "<=": (7, 8),
        ">=": (7, 8),
        "+": (10, 11),
        "-": (10, 11),
        "MOD": (12, 13),
        "\\": (13, 14),
        "*": (14, 15),
        "/": (14, 15),
        "^": (17, 17),
    }

    def __init__(self, vm: "VM", text: str):
        self.vm = vm
        self.tokens = tokenize(text)
        self.position = 0

    def peek(self) -> str:
        return self.tokens[self.position][1].upper()

    def take(self) -> tuple[str, str]:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def expect(self, wanted: str) -> None:
        if self.peek() != wanted:
            raise BasicError(f"Ожидалось {wanted}")
        self.take()

    def parse(self) -> Value:
        result = self.expression(0)
        if self.tokens[self.position][0] != "end":
            raise BasicError(f"Лишняя часть выражения: {self.peek()}")
        return result

    def expression(self, minimum: int) -> Value:
        kind, raw = self.take()
        token = raw.upper()

        if token in ("+", "-", "NOT"):
            value = self.expression(6 if token == "NOT" else 16)
            if token == "+":
                left = +value
            elif token == "-":
                left = -value
            else:
                left = ~int(value)

        elif kind == "number":
            left = numeric_literal(raw)

        elif kind == "string":
            left = literal_string(raw)

        elif token == "(":
            left = self.expression(0)
            self.expect(")")

        elif kind == "name":
            if self.peek() == "(":
                self.take()
                args = []

                if self.peek() != ")":
                    while True:
                        args.append(self.expression(0))
                        if self.peek() != ",":
                            break
                        self.take()

                self.expect(")")

                if token in self.vm.functions:
                    left = self.vm.functions[token](*args)
                else:
                    left = self.vm.array_get(token, args)
            else:
                left = self.vm.variable_get(token)

        else:
            raise BasicError("Ожидалось выражение")

        while self.peek() in self.OPERATORS:
            operator = self.peek()
            left_binding, right_binding = self.OPERATORS[operator]

            if left_binding < minimum:
                break

            self.take()
            right = self.expression(right_binding)
            left = self.apply(operator, left, right)

        return left

    @staticmethod
    def apply(operator: str, a: Value, b: Value) -> Value:
        if operator == "+":
            return a + b
        if operator == "-":
            return a - b
        if operator == "*":
            return a * b
        if operator == "/":
            return a / b
        if operator == "^":
            result = a ** b
            if isinstance(result, complex):
                raise BasicError("Комплексные числа не поддерживаются")
            return result
        if operator in ("\\", "MOD"):
            a, b = round(a), round(b)
            if b == 0:
                raise BasicError("Деление на ноль")
            quotient = abs(a) // abs(b)
            if (a < 0) != (b < 0):
                quotient = -quotient
            return quotient if operator == "\\" else a - quotient * b

        comparisons = {
            "=": lambda: a == b,
            "<>": lambda: a != b,
            "<": lambda: a < b,
            ">": lambda: a > b,
            "<=": lambda: a <= b,
            ">=": lambda: a >= b,
        }
        if operator in comparisons:
            return -int(comparisons[operator]())

        a, b = int(a), int(b)
        return {
            "AND": lambda: a & b,
            "OR": lambda: a | b,
            "XOR": lambda: a ^ b,
            "EQV": lambda: ~(a ^ b),
            "IMP": lambda: (~a) | b,
        }[operator]()


@dataclass(frozen=True)
class Instruction:
    address: Address
    text: str


class Program:
    """Compiled listing and its structural metadata."""

    def __init__(self, source: dict[int, str]):
        self.source = dict(sorted(source.items()))
        self.instructions: list[Instruction] = []
        self.addresses: list[Address] = []
        self.labels: dict[str, Address] = {}
        self.pairs: dict[Address, Address] = {}
        self.else_for: dict[Address, Address] = {}
        self.data: list[Value] = []
        self.data_at: dict[Address, int] = {}
        self.incomplete: list[str] = []

        self.compile()

    @staticmethod
    def block_kind(text: str) -> tuple[str, str] | None:
        upper = text.upper()

        if re.fullmatch(r"IF\s+.+\s+THEN", upper):
            return "open", "IF"
        if upper.startswith("FOR "):
            return "open", "FOR"
        if upper.startswith("WHILE "):
            return "open", "WHILE"
        if upper == "DO" or upper.startswith("DO "):
            return "open", "DO"

        if upper == "END IF":
            return "close", "IF"
        if upper == "NEXT" or upper.startswith("NEXT "):
            return "close", "FOR"
        if upper == "WEND":
            return "close", "WHILE"
        if upper == "LOOP" or upper.startswith("LOOP "):
            return "close", "DO"

        return None

    def compile(self) -> None:
        stack: list[tuple[str, Address]] = []

        for number, original in self.source.items():
            if not isinstance(number, int) or not 1 <= number < sys.maxsize:
                raise BasicError("Номер строки должен быть положительным")
            if not isinstance(original, str) or "\n" in original or "\r" in original:
                raise BasicError("Строка листинга не должна содержать перевод строки")

            text = strip_comment(original).strip()

            while True:
                match = re.match(r"^(" + IDENT + r")\s*:\s*", text)
                if not match:
                    break

                label = match.group(1).upper()
                if label in self.labels:
                    raise BasicError(f"Повторная метка: {label}")
                self.labels[label] = (number, 0)
                text = text[match.end():]

            for ordinal, statement in enumerate(split_statements(text)):
                address = (number, ordinal)
                self.instructions.append(Instruction(address, statement))
                self.addresses.append(address)
                self.data_at[address] = len(self.data)

                upper = statement.upper()

                if upper.startswith("DATA "):
                    for item in split_outside(statement[5:]):
                        if item.startswith('"'):
                            value = literal_string(item)
                        else:
                            try:
                                value = numeric_literal(item)
                            except BasicError:
                                value = item
                        self.data.append(value)

                if upper == "ELSE":
                    if not stack or stack[-1][0] != "IF":
                        self.incomplete.append(f"{number}: ELSE без IF")
                    else:
                        opening = stack[-1][1]
                        if opening in self.else_for:
                            self.incomplete.append(f"{number}: повторный ELSE")
                        self.else_for[opening] = address
                    continue

                block = self.block_kind(statement)
                if not block:
                    continue

                direction, kind = block

                if direction == "open":
                    stack.append((kind, address))
                elif not stack or stack[-1][0] != kind:
                    self.incomplete.append(f"{number}: неожиданный конец {kind}")
                else:
                    _, opening = stack.pop()
                    self.pairs[opening] = address
                    self.pairs[address] = opening

        for kind, address in stack:
            self.incomplete.append(f"{address[0]}: не закрыт блок {kind}")

        self.data_at[ADDRESS_END] = len(self.data)

    def validate(self) -> None:
        if self.incomplete:
            raise BasicError("; ".join(self.incomplete))

    def at_or_after(self, address: Address) -> Address:
        index = bisect.bisect_left(self.addresses, address)
        return self.addresses[index] if index < len(self.addresses) else ADDRESS_END

    def after(self, address: Address) -> Address:
        index = bisect.bisect_right(self.addresses, address)
        return self.addresses[index] if index < len(self.addresses) else ADDRESS_END

    def instruction(self, address: Address) -> Instruction:
        index = bisect.bisect_left(self.addresses, address)
        if index >= len(self.addresses) or self.addresses[index] != address:
            raise BasicError("Некорректный адрес инструкции")
        return self.instructions[index]

    def target(self, text: str) -> Address:
        key = text.strip().upper()

        if key in self.labels:
            return self.at_or_after(self.labels[key])

        if key.isdigit() and int(key) in self.source:
            return self.at_or_after((int(key), 0))

        raise BasicError(f"Нет строки или метки: {text}")

    def listing(self) -> str:
        return "\n".join(f"{n} {text}" for n, text in self.source.items())


class Timer:
    def __init__(self, elapsed: float = 0.0, running: bool = False):
        self.elapsed = float(elapsed)
        self.started = time.monotonic() if running else None

    def value(self) -> float:
        extra = 0.0 if self.started is None else time.monotonic() - self.started
        return self.elapsed + extra

    def start(self) -> None:
        if self.started is None:
            self.started = time.monotonic()

    def stop(self) -> None:
        self.elapsed = self.value()
        self.started = None

    def reset(self) -> None:
        self.elapsed = 0.0
        if self.started is not None:
            self.started = time.monotonic()

    def dump(self) -> dict:
        return {"elapsed": self.value(), "running": self.started is not None}


class VM:
    def __init__(
        self,
        *,
        allow_shell: bool = False,
        shell_policy: ShellPolicy | None = None,
        history_limit: int = 100,
        launch_count: int = 1,
    ):
        self.program = Program({})
        self.project_name = "untitled"
        self.project_path = ""
        self.dirty = False

        self.variables: dict[str, Value] = {}
        self.arrays: dict[str, dict] = {}
        self.base = 0

        self.pc = ADDRESS_END
        self.current: Address | None = None
        self.calls: list[Address] = []
        self.loops: list[dict] = []
        self.data_pointer = 0

        self.state = "ready"
        self.run_count = 0
        self.steps = 0
        self.total_steps = 0
        self.launch_count = launch_count
        self.last_error = ""
        self.error_line = 0
        self.last_exit = 0
        self.last_output = ""

        self.birth = time.monotonic()
        self.execution_time = 0.0
        self.execution_started: float | None = None

        self.timers: dict[str, Timer] = {}
        self.rng = random.Random()
        self.last_random = 0.0

        self.files: dict[int, Any] = {}
        self.breakpoints: set[int] = set()
        self.break_address: Address | None = None

        self.shell_policy = shell_policy or ShellPolicy(enabled=allow_shell)
        self.allow_shell = self.shell_policy.enabled
        self.history_limit = history_limit
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []

        self.functions = self.make_functions()
        self.readonly = {
            "PI", "TRUE", "FALSE", "DATE$", "TIME$", "TIMER", "PROGRAM$",
            "RUNCOUNT", "LAUNCHCOUNT", "STEPS", "TOTALSTEPS", "UPTIME",
            "ELAPSED", "FREEDISK", "TOTALDISK", "CURRENTLINE", "NEXTLINE",
            "CALLDEPTH", "LOOPDEPTH", "DATAPOS", "LINECOUNT", "HISTORYCOUNT",
            "REDOCOUNT", "LASTEXIT", "LASTOUTPUT$", "LASTERROR$", "ERRORLINE",
            "CWD$", "PROJECT$", "STATE$", "VERSION$", "PID",
        }

    def evaluate(self, text: str) -> Value:
        try:
            value = Expression(self, text).parse()
            if isinstance(value, float) and not math.isfinite(value):
                raise BasicError("Результат не является конечным числом")
            return value
        except BasicError:
            raise
        except Exception as exc:
            raise BasicError(f"Выражение {text!r}: {exc}") from exc

    @staticmethod
    def default(name: str) -> Value:
        return "" if name.endswith("$") else 0

    @staticmethod
    def coerce(name: str, value: Value) -> Value:
        if name.endswith("$"):
            if not isinstance(value, str):
                raise BasicError(f"{name}: требуется строка")
            return value

        if isinstance(value, str):
            raise BasicError(f"{name}: требуется число")

        if name.endswith(("%", "&")):
            return round(value)

        return value

    def elapsed(self) -> float:
        extra = (
            0.0
            if self.execution_started is None
            else time.monotonic() - self.execution_started
        )
        return self.execution_time + extra

    def system_value(self, name: str) -> Value:
        now = dt.datetime.now()
        current_line = self.current[0] if self.current else 0
        next_line = 0 if self.pc == ADDRESS_END else self.pc[0]

        getters = {
            "PI": lambda: math.pi,
            "TRUE": lambda: -1,
            "FALSE": lambda: 0,
            "DATE$": lambda: now.strftime("%m-%d-%Y"),
            "TIME$": lambda: now.strftime("%H:%M:%S"),
            "TIMER": lambda: (
                now.hour * 3600 + now.minute * 60 +
                now.second + now.microsecond / 1_000_000
            ),
            "PROGRAM$": self.program.listing,
            "RUNCOUNT": lambda: self.run_count,
            "LAUNCHCOUNT": lambda: self.launch_count,
            "STEPS": lambda: self.steps,
            "TOTALSTEPS": lambda: self.total_steps,
            "UPTIME": lambda: time.monotonic() - self.birth,
            "ELAPSED": self.elapsed,
            "FREEDISK": lambda: shutil.disk_usage(Path.cwd()).free,
            "TOTALDISK": lambda: shutil.disk_usage(Path.cwd()).total,
            "CURRENTLINE": lambda: current_line,
            "NEXTLINE": lambda: next_line,
            "CALLDEPTH": lambda: len(self.calls),
            "LOOPDEPTH": lambda: len(self.loops),
            "DATAPOS": lambda: self.data_pointer,
            "LINECOUNT": lambda: len(self.program.source),
            "HISTORYCOUNT": lambda: len(self.undo_stack),
            "REDOCOUNT": lambda: len(self.redo_stack),
            "LASTEXIT": lambda: self.last_exit,
            "LASTOUTPUT$": lambda: self.last_output,
            "LASTERROR$": lambda: self.last_error,
            "ERRORLINE": lambda: self.error_line,
            "CWD$": lambda: str(Path.cwd()),
            "PROJECT$": lambda: self.project_name,
            "STATE$": lambda: self.state,
            "VERSION$": lambda: VERSION,
            "PID": os.getpid,
        }
        return getters[name]()

    def variable_get(self, name: str) -> Value:
        name = canonical(name)

        if name in self.readonly:
            return self.system_value(name)
        if name == "RND":
            return self.random_number()
        if name == "INKEY$":
            return self.inkey()

        return self.variables.get(name, self.default(name))

    def lvalue(self, text: str) -> tuple[str, list[int] | None]:
        match = re.fullmatch(
            r"\s*(" + IDENT + r")\s*(?:\((.*)\))?\s*",
            text,
        )
        if not match:
            raise BasicError(f"Некорректная переменная: {text}")

        name, indices = match.groups()
        name = canonical(name)

        if name in self.readonly or name in self.functions:
            raise BasicError(f"Зарезервированное имя: {name}")

        return name, (
            None if indices is None else
            [int(self.evaluate(x)) for x in split_outside(indices)]
        )

    def array_cell(self, name: str, indices: list) -> tuple[dict, str]:
        name = canonical(name)
        indices = [int(x) for x in indices]

        if not indices:
            raise BasicError("Массив требует индексы")

        if name not in self.arrays:
            self.arrays[name] = {
                "bounds": [[self.base, 10] for _ in indices],
                "values": {},
            }

        array = self.arrays[name]

        if len(indices) != len(array["bounds"]):
            raise BasicError("Неверное число индексов массива")

        for index, bounds in zip(indices, array["bounds"]):
            low, high = bounds
            if not low <= index <= high:
                raise BasicError(f"{name}: индекс {index} вне {low}..{high}")

        return array, ",".join(map(str, indices))

    def array_get(self, name: str, indices: list) -> Value:
        array, key = self.array_cell(name, indices)
        return array["values"].get(key, self.default(name))

    def get(self, target: str) -> Value:
        name, indices = self.lvalue(target)
        if indices is None:
            return self.variable_get(name)
        return self.array_get(name, indices)

    def assign(self, target: str, value: Value) -> None:
        name, indices = self.lvalue(target)
        value = self.coerce(name, value)

        if indices is None:
            self.variables[name] = value
        else:
            array, key = self.array_cell(name, indices)
            array["values"][key] = value

    def bound(self, name: str, dimension: int = 1, upper: bool = False) -> int:
        array = self.arrays.get(canonical(str(name)))
        dimension = int(dimension)

        if array is None or not 1 <= dimension <= len(array["bounds"]):
            raise BasicError("Нет массива или измерения")

        return array["bounds"][dimension - 1][int(upper)]

    def random_number(self, argument: float = 1) -> float:
        if argument < 0:
            self.rng.seed(argument)
        if argument != 0:
            self.last_random = self.rng.random()
        return self.last_random

    @staticmethod
    def inkey() -> str:
        if os.name == "nt":
            import msvcrt
            return msvcrt.getwch() if msvcrt.kbhit() else ""

        if not sys.stdin.isatty():
            return ""

        import select
        import termios
        import tty

        old = termios.tcgetattr(sys.stdin)

        try:
            tty.setcbreak(sys.stdin.fileno())
            return sys.stdin.read(1) if select.select([sys.stdin], [], [], 0)[0] else ""
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)

    @staticmethod
    def environment(key: str | int) -> str:
        if isinstance(key, str):
            return os.environ.get(key, "")

        entries = list(os.environ.items())
        index = int(key) - 1
        return "=".join(entries[index]) if 0 <= index < len(entries) else ""

    def make_functions(self) -> dict:
        def length(n):
            n = int(n)
            if n < 0:
                raise BasicError("Длина не может быть отрицательной")
            return n

        def mid(text, start, count=None):
            start = int(start)
            if start < 1:
                raise BasicError("MID$: начальная позиция меньше 1")
            return (
                text[start - 1:]
                if count is None
                else text[start - 1:start - 1 + length(count)]
            )

        def right(text, count):
            count = length(count)
            return text[-count:] if count else ""

        def instr(*args):
            if len(args) == 2:
                start, text, needle = 1, args[0], args[1]
            elif len(args) == 3:
                start, text, needle = args
            else:
                raise BasicError("INSTR: требуется 2 или 3 аргумента")

            start = int(start)
            if start < 1:
                raise BasicError("INSTR: начальная позиция меньше 1")
            return text.find(needle, start - 1) + 1

        def val(text):
            match = re.match(
                r"\s*[+-]?(?:&[Hh][0-9A-Fa-f]+|&[Oo][0-7]+|"
                r"(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?)",
                str(text),
            )
            return numeric_literal(match.group()) if match else 0

        def sys_value(key):
            key = canonical(str(key))
            aliases = {
                "CWD": "CWD$", "VERSION": "VERSION$", "PROGRAM": "PROJECT$",
                "STATE": "STATE$", "LINE": "CURRENTLINE", "PC": "NEXTLINE",
                "DATE": "DATE$", "TIME": "TIME$",
            }
            key = aliases.get(key, key)

            if key == "OS":
                return platform.platform()
            if key == "PYTHON":
                return platform.python_version()
            if key not in self.readonly:
                raise BasicError(f"Неизвестный системный показатель: {key}")

            return self.system_value(key)

        functions = {
            "ABS": abs, "ATN": math.atan, "COS": math.cos,
            "SIN": math.sin, "TAN": math.tan, "SQR": math.sqrt,
            "EXP": math.exp, "LOG": math.log, "INT": math.floor,
            "FIX": math.trunc, "CINT": round, "CLNG": round,
            "CSNG": float, "CDBL": float,
            "SGN": lambda x: (x > 0) - (x < 0),
            "MIN": lambda *x: min(x), "MAX": lambda *x: max(x),
            "ROUND": round, "FLOOR": math.floor, "CEIL": math.ceil,
            "RND": self.random_number,
            "LEN": len, "ASC": lambda s: ord(s[0]),
            "CHR$": lambda n: chr(int(n)),
            "STR$": lambda n: (" " if n >= 0 else "") + display(n),
            "VAL": val,
            "HEX$": lambda n: format(int(n), "X"),
            "OCT$": lambda n: format(int(n), "o"),
            "LEFT$": lambda s, n: s[:length(n)],
            "RIGHT$": right, "MID$": mid,
            "LCASE$": lambda s: s.lower(),
            "UCASE$": lambda s: s.upper(),
            "LTRIM$": lambda s: s.lstrip(),
            "RTRIM$": lambda s: s.rstrip(),
            "SPACE$": lambda n: " " * length(n),
            "STRING$": lambda n, c: (
                c[:1] if isinstance(c, str) else chr(int(c))
            ) * length(n),
            "INSTR": instr,
            "TAB": lambda n: " " * max(0, int(n) - 1),
            "SPC": lambda n: " " * length(n),
            "ENVIRON$": self.environment,
            "SYS": sys_value,
            "SYS$": lambda key: display(sys_value(key)),
            "INKEY$": self.inkey,
            "LINE$": lambda n: self.program.source.get(int(n), ""),
            "LINEEXISTS": lambda n: -int(int(n) in self.program.source),
            "FINDLINE": self.find_line,
            "LBOUND": lambda n, d=1: self.bound(n, d),
            "UBOUND": lambda n, d=1: self.bound(n, d, True),
            "VAREXISTS": lambda n: -int(
                canonical(str(n)) in self.variables
                or canonical(str(n)) in self.arrays
                or canonical(str(n)) in self.readonly
            ),
            "TIMERGET": lambda n: self.timer(str(n)).value(),
            "FILEEXISTS": lambda p: -int(Path(p).is_file()),
            "DIREXISTS": lambda p: -int(Path(p).is_dir()),
            "FILESIZE": lambda p: Path(p).stat().st_size,
            "READFILE$": lambda p: Path(p).read_text(encoding="utf-8"),
            "EOF": self.eof,
            "LOF": lambda n: os.fstat(self.file(n).fileno()).st_size,
            "LOC": lambda n: self.file(n).tell(),
            "SEEK": lambda n: self.file(n).tell() + 1,
            "SHELL": lambda command: self.shell(command, capture=False),
            "SHELL$": lambda command: self.shell(command, capture=True),
        }
        functions.update({name.upper(): function for name, function in FUNCTIONS.items()})
        return functions

    def timer(self, name: str) -> Timer:
        return self.timers.setdefault(canonical(name), Timer())

    def find_line(self, needle: str, start: int = 1, sensitive: int = 0) -> int:
        for number, text in self.program.source.items():
            if number < int(start):
                continue
            haystack = text if sensitive else text.casefold()
            query = needle if sensitive else needle.casefold()
            if query in haystack:
                return number
        return 0

    def shell(self, command: str, capture: bool = False) -> int | str:
        if not isinstance(command, str):
            raise BasicError("SHELL требует строку")
        try:
            self.shell_policy.check(command)
        except Exception as exc:
            raise BasicError(str(exc)) from exc

        try:
            result = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=capture,
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise BasicError(f"Не удалось выполнить SHELL: {exc}") from exc
        self.last_exit = result.returncode
        self.last_output = result.stdout if capture else ""
        return self.last_output if capture else self.last_exit

    # ---------- Source mutation ----------

    def source_record(self, description: str) -> dict:
        return {
            "source": dict(self.program.source),
            "description": description,
            "time": dt.datetime.now().isoformat(timespec="seconds"),
        }

    def check_replacement(self, candidate: Program) -> None:
        # During a live execution, incomplete control-flow blocks are unsafe.
        live = self.state in ("running", "paused")

        if live:
            candidate.validate()

        # A running FOR/DO frame depends on the exact opening statement.
        # Changes to its body are permitted. Removing/changing its header is not.
        for frame in self.loops:
            address = frame["start"]

            try:
                old = self.program.instruction(address).text
                new = candidate.instruction(address).text
            except BasicError as exc:
                raise BasicError(
                    "Изменение удаляет заголовок активного цикла"
                ) from exc

            if old != new or address not in candidate.pairs:
                raise BasicError(
                    "Нельзя менять заголовок активного цикла; сначала выйдите из него"
                )

    def install_source(self, candidate: Program) -> None:
        was_empty = not self.program.instructions
        self.program = candidate

        # Execution resumes from the first surviving instruction at or after
        # the previous continuation address. Earlier inserted lines are not
        # executed retroactively.
        if self.state in ("running", "paused"):
            self.pc = candidate.at_or_after(self.pc)
            self.calls = [candidate.at_or_after(a) for a in self.calls]
        else:
            self.pc = candidate.at_or_after((1, 0))
            self.calls.clear()
            self.loops.clear()
            self.state = "ready"

        if was_empty and self.state == "ready":
            self.pc = candidate.at_or_after((1, 0))

        # DATA is addressed by ordinal; mutation does not reset it.
        self.data_pointer = min(self.data_pointer, len(candidate.data))
        self.dirty = True

    def mutate(self, source: dict[int, str], description: str) -> None:
        if source == self.program.source:
            return

        candidate = Program(source)
        self.check_replacement(candidate)

        old = self.source_record(description)
        self.install_source(candidate)

        self.undo_stack.append(old)
        self.undo_stack = self.undo_stack[-self.history_limit:]
        self.redo_stack.clear()

    def undo(self, count: int = 1, redo: bool = False) -> None:
        count = int(count)
        if count < 1:
            raise BasicError("Число шагов должно быть положительным")

        source_stack = self.redo_stack if redo else self.undo_stack
        destination_stack = self.undo_stack if redo else self.redo_stack

        if count > len(source_stack):
            raise BasicError(f"Доступно шагов: {len(source_stack)}")

        # Validate the final result before changing anything.
        target = source_stack[-count]
        candidate = Program(target["source"])
        self.check_replacement(candidate)

        current = self.source_record("redo" if redo else "undo")
        for _ in range(count):
            record = source_stack.pop()
            destination_stack.append(current)
            current = record

        destination_stack[:] = destination_stack[-self.history_limit:]
        self.install_source(candidate)

    def source_statement(self, body: str) -> None:
        pieces = body.split(None, 1)
        action = pieces[0].upper() if pieces else ""
        rest = pieces[1] if len(pieces) > 1 else ""

        if action in ("UNDO", "REDO"):
            count = int(self.evaluate(rest)) if rest else 1
            self.undo(count, redo=action == "REDO")
            return

        args = [self.evaluate(x) for x in split_outside(rest)] if rest else []
        source = dict(self.program.source)

        if action in ("SET", "INSERT"):
            if len(args) != 2 or not isinstance(args[1], str):
                raise BasicError("SOURCE SET/INSERT номер, строка")
            number, text = int(args[0]), args[1]
            if action == "INSERT" and number in source:
                raise BasicError("Номер строки уже занят")
            source[number] = text

        elif action == "DELETE":
            if not 1 <= len(args) <= 2:
                raise BasicError("SOURCE DELETE от [, до]")
            low = int(args[0])
            high = int(args[-1])
            if high < low:
                raise BasicError("Неверный диапазон")
            source = {n: s for n, s in source.items() if not low <= n <= high}

        elif action == "REPLACE":
            if not 2 <= len(args) <= 5:
                raise BasicError(
                    "SOURCE REPLACE что, на_что [, от, до, учитывать_регистр]"
                )
            old, new = args[:2]
            if not isinstance(old, str) or not old or not isinstance(new, str):
                raise BasicError("Требуются две строки; образец не может быть пуст")
            low = int(args[2]) if len(args) >= 3 else 1
            high = int(args[3]) if len(args) >= 4 else sys.maxsize
            sensitive = bool(args[4]) if len(args) >= 5 else False
            pattern = re.compile(re.escape(old), 0 if sensitive else re.I)

            source = {
                n: pattern.sub(lambda _: new, s) if low <= n <= high else s
                for n, s in source.items()
            }

        else:
            raise BasicError("SOURCE: SET, INSERT, DELETE, REPLACE, UNDO, REDO")

        self.mutate(source, f"SOURCE {action}")

    # ---------- Files and console ----------

    def file(self, number: int):
        number = int(number)
        if number not in self.files:
            raise BasicError(f"Файл #{number} не открыт")
        return self.files[number]

    def close_files(self) -> None:
        for handle in self.files.values():
            handle.close()
        self.files.clear()

    def eof(self, number: int) -> int:
        handle = self.file(number)
        position = handle.tell()
        char = handle.read(1)
        handle.seek(position)
        return -int(char == "")

    def destination(self, body: str) -> tuple[Any, str]:
        body = body.strip()
        if not body.startswith("#"):
            return None, body

        pieces = split_outside(body)
        if len(pieces) < 2:
            raise BasicError("После номера файла нужна запятая")

        return self.file(self.evaluate(pieces[0][1:])), ",".join(pieces[1:])

    def print_statement(self, body: str, write: bool = False) -> None:
        handle, body = self.destination(body)
        output = handle or sys.stdout

        if write:
            values = [self.evaluate(x) for x in split_outside(body)] if body else []
            fields = [
                '"' + value.replace('"', '""') + '"'
                if isinstance(value, str) else display(value)
                for value in values
            ]
            output.write(",".join(fields) + "\n")
            output.flush()
            return

        # Split by both separators, preserving their order.
        parts = []
        quoted = False
        depth = 0
        start = 0
        i = 0

        while i < len(body):
            char = body[i]
            if char == '"':
                if quoted and i + 1 < len(body) and body[i + 1] == '"':
                    i += 2
                    continue
                quoted = not quoted
            elif not quoted:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                elif char in ";," and depth == 0:
                    parts.append((body[start:i].strip(), char))
                    start = i + 1
            i += 1

        parts.append((body[start:].strip(), ""))

        for expression, separator in parts:
            if expression:
                output.write(display(self.evaluate(expression)))
            if separator == ",":
                output.write("\t")

        if not body.rstrip().endswith((";", ",")):
            output.write("\n")
        output.flush()

    def input_statement(self, body: str, whole: bool = False) -> None:
        handle, body = self.destination(body)
        prompt = "" if whole else "? "

        if handle is None and body.startswith('"'):
            match = re.match(r'^("(?:[^"]|"")*")\s*([;,])\s*(.*)$', body)
            if not match:
                raise BasicError("Некорректный INPUT prompt")
            prompt = literal_string(match.group(1))
            if match.group(2) == ";":
                prompt += "? "
            body = match.group(3)

        if handle is not None:
            line = handle.readline()
            if not line:
                raise BasicError("Конец файла")
            line = line.rstrip("\r\n")
        else:
            line = input(prompt)

        targets = split_outside(body)

        if whole:
            if len(targets) != 1 or not self.lvalue(targets[0])[0].endswith("$"):
                raise BasicError("LINE INPUT требует одну строковую переменную")
            self.assign(targets[0], line)
            return

        values = next(csv.reader([line], skipinitialspace=True))
        if len(values) != len(targets):
            raise BasicError("Число значений INPUT не совпадает")

        converted = []
        for target, value in zip(targets, values):
            name, _ = self.lvalue(target)
            converted.append(
                (target, value if name.endswith("$") else numeric_literal(value))
            )

        for target, value in converted:
            self.assign(target, value)

    # ---------- Execution ----------

    def start(self, target: str | None = None) -> None:
        self.program.validate()
        self.close_files()
        self.variables.clear()
        self.arrays.clear()
        self.base = 0
        self.calls.clear()
        self.loops.clear()
        self.data_pointer = 0
        self.steps = 0
        self.execution_time = 0.0
        self.last_error = ""
        self.error_line = 0
        self.current = None
        self.break_address = None
        self.run_count += 1
        self.pc = (
            self.program.target(target)
            if target else self.program.at_or_after((1, 0))
        )
        self.state = "ready"

    def require_program(self, address: Address | None) -> Address:
        if address is None:
            raise BasicError("Эта инструкция доступна только в листинге")
        return address

    def paired(self, address: Address) -> Address:
        if address not in self.program.pairs:
            raise BasicError("Не найден конец/начало блока")
        return self.program.pairs[address]

    def suffix_condition(self, text: str) -> bool:
        text = text.strip()
        if not text:
            return True

        match = re.fullmatch(r"(WHILE|UNTIL)\s+(.+)", text, re.I)
        if not match:
            raise BasicError("Ожидалось WHILE/UNTIL выражение")

        value = bool(self.evaluate(match.group(2)))
        return value if match.group(1).upper() == "WHILE" else not value

    def execute(self, text: str, address: Address | None = None) -> None:
        text = text.strip()
        upper = text.upper()

        if not text or upper == "REM" or upper.startswith(("REM ", "DATA ")):
            return

        if text.startswith("?"):
            self.print_statement(text[1:].strip())
            return

        first = re.match(r"[A-Za-z]+", text)
        command = first.group().upper() if first else ""
        body = text[len(command):].strip()

        if command == "PRINT":
            self.print_statement(body)
            return
        if command == "WRITE":
            self.print_statement(body, write=True)
            return
        if upper.startswith("LINE INPUT "):
            self.input_statement(text[11:].strip(), whole=True)
            return
        if command == "INPUT":
            self.input_statement(body)
            return
        if command == "SOURCE":
            self.source_statement(body)
            return

        if command == "IF":
            then = keyword(body, "THEN")
            if then < 0:
                raise BasicError("IF без THEN")

            condition = bool(self.evaluate(body[:then]))
            tail = body[then + 4:].strip()

            if not tail:
                address = self.require_program(address)
                if not condition:
                    destination = self.program.else_for.get(
                        address, self.paired(address)
                    )
                    self.pc = self.program.after(destination)
                return

            if keyword(tail, "IF") >= 0:
                raise BasicError("Вложенный однострочный IF не поддерживается")

            else_at = keyword(tail, "ELSE")
            yes = tail if else_at < 0 else tail[:else_at]
            no = "" if else_at < 0 else tail[else_at + 4:]
            branch = (yes if condition else no).strip()

            if not branch:
                return

            if branch.isdigit() or canonical(branch) in self.program.labels:
                self.pc = self.program.target(branch)
                return

            for statement in split_statements(branch):
                previous_pc = self.pc
                self.execute(statement, address)
                if self.pc != previous_pc or self.state != "running":
                    break
            return

        if upper == "ELSE":
            address = self.require_program(address)
            opening = next(
                (a for a, b in self.program.else_for.items() if b == address),
                None,
            )
            if opening is None:
                raise BasicError("ELSE без IF")
            self.pc = self.program.after(self.paired(opening))
            return

        if upper == "END IF":
            return

        if command in ("GOTO", "GOSUB"):
            self.require_program(address)
            target = self.program.target(body)
            if command == "GOSUB":
                self.calls.append(self.pc)
            self.pc = target
            return

        if upper == "RETURN":
            if not self.calls:
                raise BasicError("RETURN без GOSUB")
            self.pc = self.program.at_or_after(self.calls.pop())
            return

        if command == "ON":
            match = re.fullmatch(r"(.+?)\s+(GOTO|GOSUB)\s+(.+)", body, re.I)
            if not match:
                raise BasicError("ON expr GOTO/GOSUB targets")
            self.require_program(address)
            index = int(self.evaluate(match.group(1)))
            targets = split_outside(match.group(3))
            if 1 <= index <= len(targets):
                destination = self.program.target(targets[index - 1])
                if match.group(2).upper() == "GOSUB":
                    self.calls.append(self.pc)
                self.pc = destination
            return

        if command == "FOR":
            address = self.require_program(address)
            match = re.fullmatch(
                r"(" + IDENT + r")\s*=\s*(.*?)\s+TO\s+(.+)",
                body,
                re.I,
            )
            if not match:
                raise BasicError("Некорректный FOR")

            name, initial, tail = match.groups()
            split = keyword(tail, "STEP")
            limit = self.evaluate(tail if split < 0 else tail[:split])
            step = 1 if split < 0 else self.evaluate(tail[split + 4:])
            initial = self.evaluate(initial)

            if step == 0:
                raise BasicError("STEP не может быть 0")
            if name.endswith("$"):
                raise BasicError("Счётчик FOR должен быть числовым")

            self.assign(name, initial)
            if (step > 0 and initial > limit) or (step < 0 and initial < limit):
                self.pc = self.program.after(self.paired(address))
            else:
                self.loops.append({
                    "kind": "FOR", "start": address,
                    "name": canonical(name), "limit": limit, "step": step,
                })
            return

        if command == "NEXT":
            address = self.require_program(address)
            if not self.loops or self.loops[-1]["kind"] != "FOR":
                raise BasicError("NEXT без активного FOR")

            frame = self.loops[-1]
            if self.paired(address) != frame["start"]:
                raise BasicError("NEXT не соответствует активному FOR")
            if body and canonical(body) != frame["name"]:
                raise BasicError("NEXT: неверный счётчик")

            value = self.variable_get(frame["name"]) + frame["step"]
            self.assign(frame["name"], value)
            keep = (
                value <= frame["limit"] if frame["step"] > 0
                else value >= frame["limit"]
            )
            if keep:
                self.pc = self.program.after(frame["start"])
            else:
                self.loops.pop()
            return

        if command == "WHILE":
            address = self.require_program(address)
            if not self.evaluate(body):
                self.pc = self.program.after(self.paired(address))
            return

        if upper == "WEND":
            self.pc = self.paired(self.require_program(address))
            return

        if command == "DO":
            address = self.require_program(address)
            if not self.suffix_condition(body):
                self.pc = self.program.after(self.paired(address))
            else:
                self.loops.append({"kind": "DO", "start": address})
            return

        if command == "LOOP":
            address = self.require_program(address)
            if not self.loops or self.loops[-1]["kind"] != "DO":
                raise BasicError("LOOP без активного DO")
            frame = self.loops[-1]
            if self.paired(address) != frame["start"]:
                raise BasicError("LOOP не соответствует активному DO")
            keep = self.suffix_condition(body)
            self.loops.pop()
            if keep:
                self.pc = frame["start"]
            return

        if upper in ("EXIT FOR", "EXIT DO"):
            kind = upper.split()[1]
            for index in range(len(self.loops) - 1, -1, -1):
                frame = self.loops[index]
                if frame["kind"] == kind:
                    self.pc = self.program.after(self.paired(frame["start"]))
                    del self.loops[index:]
                    return
            raise BasicError(f"{upper} вне цикла")

        if upper.startswith("OPTION BASE "):
            value = int(self.evaluate(text[12:]))
            if value not in (0, 1):
                raise BasicError("OPTION BASE: допустимы 0 и 1")
            self.base = value
            return

        if command == "DIM":
            additions = {}
            for declaration in split_outside(body):
                match = re.fullmatch(
                    r"(" + IDENT + r")\s*\((.*)\)",
                    declaration,
                )
                if not match:
                    raise BasicError("DIM требует массив с границами")

                name = canonical(match.group(1))
                self.lvalue(name)

                if name in self.arrays or name in additions:
                    raise BasicError(f"{name}: массив уже существует")

                bounds = []
                for item in split_outside(match.group(2)):
                    at = keyword(item, "TO")
                    low = self.base if at < 0 else int(self.evaluate(item[:at]))
                    high = int(self.evaluate(item if at < 0 else item[at + 2:]))
                    if low > high:
                        raise BasicError("Некорректные границы массива")
                    bounds.append([low, high])

                additions[name] = {"bounds": bounds, "values": {}}

            self.arrays.update(additions)
            return

        if command == "ERASE":
            for name in split_outside(body):
                self.arrays.pop(canonical(name), None)
            return

        if command == "SWAP":
            args = split_outside(body)
            if len(args) != 2:
                raise BasicError("SWAP требует две переменные")
            a, b = args
            av, bv = self.get(a), self.get(b)
            self.coerce(self.lvalue(a)[0], bv)
            self.coerce(self.lvalue(b)[0], av)
            self.assign(a, bv)
            self.assign(b, av)
            return

        if command == "READ":
            targets = split_outside(body)
            if self.data_pointer + len(targets) > len(self.program.data):
                raise BasicError("Недостаточно DATA")
            for target in targets:
                self.assign(target, self.program.data[self.data_pointer])
                self.data_pointer += 1
            return

        if command == "RESTORE":
            target = self.program.target(body) if body else None
            self.data_pointer = 0 if target is None else self.program.data_at[target]
            return

        if command == "RANDOMIZE":
            self.rng.seed(self.evaluate(body) if body else time.time_ns())
            return

        if command == "OPEN":
            match = re.fullmatch(
                r"(.+)\s+FOR\s+(INPUT|OUTPUT|APPEND)\s+AS\s+#?(.+)",
                body,
                re.I,
            )
            if not match:
                raise BasicError('OPEN "file" FOR INPUT|OUTPUT|APPEND AS #n')
            path, mode, number = match.groups()
            number = int(self.evaluate(number))
            if number <= 0 or number in self.files:
                raise BasicError("Неверный или занятый номер файла")
            mode = {"INPUT": "r", "OUTPUT": "w", "APPEND": "a"}[mode.upper()]
            self.files[number] = open(
                str(self.evaluate(path)), mode, encoding="utf-8", newline=""
            )
            return

        if command == "CLOSE":
            if not body:
                self.close_files()
            else:
                for item in split_outside(body):
                    number = int(self.evaluate(item.lstrip("#")))
                    self.file(number).close()
                    del self.files[number]
            return

        if command == "SEEK":
            args = split_outside(body)
            if len(args) != 2:
                raise BasicError("SEEK #n, position")
            number, position = args
            self.file(self.evaluate(number.lstrip("#"))).seek(
                int(self.evaluate(position)) - 1
            )
            return

        if command in ("WRITEFILE", "APPENDFILE", "COPYFILE"):
            args = [self.evaluate(x) for x in split_outside(body)]
            if len(args) != 2:
                raise BasicError(f"{command} требует два аргумента")
            if command == "COPYFILE":
                shutil.copy2(args[0], args[1])
            else:
                mode = "w" if command == "WRITEFILE" else "a"
                with open(args[0], mode, encoding="utf-8") as handle:
                    handle.write(str(args[1]))
            return

        if command in ("CHDIR", "MKDIR", "RMDIR", "KILL"):
            path = str(self.evaluate(body))
            {
                "CHDIR": os.chdir, "MKDIR": os.mkdir,
                "RMDIR": os.rmdir, "KILL": os.remove,
            }[command](path)
            return

        if command == "NAME":
            at = keyword(body, "AS")
            if at < 0:
                raise BasicError("NAME old AS new")
            os.rename(
                str(self.evaluate(body[:at])),
                str(self.evaluate(body[at + 2:])),
            )
            return

        if command == "ENVIRON":
            key, value = str(self.evaluate(body)).split("=", 1)
            os.environ[key] = value
            return

        if command == "SHELL":
            self.shell(self.evaluate(body))
            return

        if command == "TIMER":
            pieces = body.split(None, 1)
            action = pieces[0].upper() if pieces else ""
            if len(pieces) != 2 or action not in ("START", "STOP", "RESET"):
                raise BasicError('TIMER START|STOP|RESET "name"')
            timer = self.timer(str(self.evaluate(pieces[1])))
            {"START": timer.start, "STOP": timer.stop, "RESET": timer.reset}[action]()
            return

        if command in ("VARSAVE", "VARLOAD", "SNAPSHOT"):
            path = str(self.evaluate(body))
            if command == "VARSAVE":
                self.save_variables(path)
            elif command == "VARLOAD":
                self.load_variables(path)
            else:
                self.save_snapshot(path)
            return

        if upper == "CLS":
            print("\033[2J\033[H", end="", flush=True)
            return
        if upper == "BEEP":
            print("\a", end="", flush=True)
            return
        if command == "SLEEP":
            if body:
                time.sleep(max(0, float(self.evaluate(body))))
            else:
                input()
            return
        if upper == "STOP":
            self.state = "paused"
            return
        if upper in ("END", "SYSTEM"):
            self.state = "ended"
            self.pc = ADDRESS_END
            self.close_files()
            return

        assignment = body if command == "LET" else text
        equals = keyword(assignment, "=")

        # "=" is punctuation; find it outside strings/parentheses instead.
        parts = split_outside(assignment, "=")
        if len(parts) == 2:
            target, expression = parts
            self.assign(target, self.evaluate(expression))
            return

        raise BasicError(f"Неизвестная или неподдерживаемая инструкция: {text}")

    def run(self, count: int | None = None, limit: int = 1_000_000, trace=False):
        if self.state == "ended":
            return

        self.program.validate()
        self.state = "running"
        self.execution_started = time.monotonic()
        executed = 0

        try:
            while self.pc != ADDRESS_END and self.state == "running":
                address = self.program.at_or_after(self.pc)
                if address == ADDRESS_END:
                    self.pc = ADDRESS_END
                    break

                instruction = self.program.instruction(address)
                line, ordinal = address

                if (
                    count is None
                    and ordinal == 0
                    and line in self.breakpoints
                    and self.break_address != address
                ):
                    self.pc = address
                    self.break_address = address
                    self.state = "paused"
                    break

                self.break_address = None

                if limit and executed >= limit:
                    self.state = "paused"
                    raise BasicError(f"Лимит {limit} инструкций; используйте CONT")

                self.current = address
                self.pc = self.program.after(address)

                if trace:
                    print(f"[{line}:{ordinal}] {instruction.text}", file=sys.stderr)

                try:
                    self.execute(instruction.text, address)
                except Exception as exc:
                    self.pc = self.program.at_or_after(address)
                    self.state = "paused"
                    self.last_error = str(exc)
                    self.error_line = line
                    raise BasicError(f"Строка {line}: {exc}") from exc

                executed += 1
                self.steps += 1
                self.total_steps += 1

                if count is not None and executed >= count:
                    if self.state == "running":
                        self.state = "paused"
                    break

            if self.pc == ADDRESS_END and self.state == "running":
                self.state = "ended"
                self.close_files()

        except KeyboardInterrupt:
            self.state = "paused"
            print("\nПрервано. CONT — продолжить.", file=sys.stderr)
        finally:
            self.execution_time = self.elapsed()
            self.execution_started = None
            self.current = None

    def immediate(self, text: str):
        previous_state = self.state

        # Source edits at the prompt use the true execution state.
        if text.strip().upper().startswith("SOURCE "):
            self.source_statement(text.strip()[7:])
            return

        self.state = "running"
        try:
            for statement in split_statements(text):
                self.execute(statement)
                if self.state != "running":
                    break
        finally:
            if self.state == "running":
                self.state = previous_state

    # ---------- Persistence ----------

    def variable_payload(self) -> dict:
        return {
            "variables": copy.deepcopy(self.variables),
            "arrays": copy.deepcopy(self.arrays),
            "base": self.base,
        }

    def validate_variables(self, obj: dict) -> tuple[dict, dict, int]:
        variables = obj["variables"]
        arrays = obj["arrays"]
        base = obj["base"]

        if not isinstance(variables, dict) or not isinstance(arrays, dict):
            raise BasicError("Некорректные переменные/массивы")
        if base not in (0, 1):
            raise BasicError("Некорректный OPTION BASE")

        clean_variables = {}
        clean_arrays = {}

        def valid_value(name, value):
            if not isinstance(value, (int, float, str)):
                raise BasicError("Некорректное значение переменной")
            if isinstance(value, float) and not math.isfinite(value):
                raise BasicError("Неконечное число")
            return self.coerce(name, value)

        def valid_name(name):
            if not isinstance(name, str) or not re.fullmatch(IDENT, name):
                raise BasicError("Некорректное имя переменной")
            result = canonical(name)
            self.lvalue(result)
            return result

        for name, value in variables.items():
            name = valid_name(name)
            if name in clean_variables:
                raise BasicError("Имена отличаются только регистром")
            clean_variables[name] = valid_value(name, value)

        for name, array in arrays.items():
            name = valid_name(name)
            if name in clean_arrays:
                raise BasicError("Имена массивов отличаются только регистром")

            bounds = array["bounds"]
            values = array["values"]

            if not isinstance(bounds, list) or not bounds or not isinstance(values, dict):
                raise BasicError("Некорректный массив")

            for pair in bounds:
                if (
                    not isinstance(pair, list)
                    or len(pair) != 2
                    or not all(isinstance(x, int) for x in pair)
                    or pair[0] > pair[1]
                ):
                    raise BasicError("Некорректные границы массива")

            clean_values = {}
            for key, value in values.items():
                indices = [int(x) for x in key.split(",")]
                if len(indices) != len(bounds) or any(
                    not low <= index <= high
                    for index, (low, high) in zip(indices, bounds)
                ):
                    raise BasicError("Некорректный индекс массива")
                clean_values[",".join(map(str, indices))] = valid_value(name, value)

            clean_arrays[name] = {"bounds": bounds, "values": clean_values}

        return clean_variables, clean_arrays, base

    def save_variables(self, path: str) -> None:
        atomic_json(path, {
            "format": "mutabasic-variables",
            "version": FORMAT_VERSION,
            **self.variable_payload(),
        })

    def load_variables(self, path: str) -> None:
        obj = read_json(path, "mutabasic-variables")
        values = self.validate_variables(obj)
        self.variables, self.arrays, self.base = values

    def project_payload(self) -> dict:
        return {
            "name": self.project_name,
            "source": {str(k): v for k, v in self.program.source.items()},
            "undo": copy.deepcopy(self.undo_stack),
            "redo": copy.deepcopy(self.redo_stack),
            "run_count": self.run_count,
        }

    def save_project(self, path: str) -> None:
        atomic_json(path, {
            "format": "mutabasic-project",
            "version": FORMAT_VERSION,
            **self.project_payload(),
        })
        self.project_path = str(Path(path).resolve())
        self.dirty = False

    def restore_project_payload(self, obj: dict) -> None:
        self.program = Program({int(k): v for k, v in obj["source"].items()})
        self.project_name = str(obj.get("name", "untitled"))
        self.run_count = int(obj.get("run_count", 0))

        for attribute, key in (("undo_stack", "undo"), ("redo_stack", "redo")):
            records = []
            for record in obj.get(key, [])[-self.history_limit:]:
                record = dict(record)
                record["source"] = {
                    int(k): v for k, v in record["source"].items()
                }
                Program(record["source"])
                records.append(record)
            setattr(self, attribute, records)

        self.pc = self.program.at_or_after((1, 0))

    def load_project(self, path: str) -> None:
        obj = read_json(path, "mutabasic-project")
        trial = VM(
            shell_policy=self.shell_policy,
            history_limit=self.history_limit,
            launch_count=self.launch_count,
        )
        trial.restore_project_payload(obj)
        trial.project_path = str(Path(path).resolve())
        trial.dirty = False

        self.close_files()
        self.__dict__.update(trial.__dict__)
        self.functions = self.make_functions()

    def save_snapshot(self, path: str) -> None:
        if self.files:
            raise BasicError("Перед снимком закройте BASIC-файлы: CLOSE")

        atomic_json(path, {
            "format": "mutabasic-snapshot",
            "version": FORMAT_VERSION,
            "project": self.project_payload(),
            **self.variable_payload(),
            "pc": self.pc,
            "calls": self.calls,
            "loops": self.loops,
            "data_pointer": self.data_pointer,
            "state": "paused" if self.state == "running" else self.state,
            "steps": self.steps,
            "total_steps": self.total_steps,
            "elapsed": self.elapsed(),
            "breakpoints": sorted(self.breakpoints),
            "timers": {name: timer.dump() for name, timer in self.timers.items()},
            "random_state": self.rng.getstate(),
            "last_random": self.last_random,
            "last_exit": self.last_exit,
            "last_output": self.last_output,
        })

    def load_snapshot(self, path: str) -> None:
        if self.files:
            raise BasicError("Перед восстановлением закройте BASIC-файлы")

        obj = read_json(path, "mutabasic-snapshot")
        trial = VM(
            shell_policy=self.shell_policy,
            history_limit=self.history_limit,
            launch_count=self.launch_count,
        )
        trial.restore_project_payload(obj["project"])
        trial.variables, trial.arrays, trial.base = trial.validate_variables(obj)

        def address(value):
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise BasicError("Некорректный адрес в снимке")
            result = tuple(value)
            if not all(isinstance(x, int) for x in result):
                raise BasicError("Некорректный адрес в снимке")
            if result != ADDRESS_END and result not in trial.program.addresses:
                raise BasicError("Адрес отсутствует в листинге")
            return result

        trial.pc = address(obj["pc"])
        trial.calls = [address(x) for x in obj["calls"]]
        trial.loops = copy.deepcopy(obj["loops"])

        for frame in trial.loops:
            frame["start"] = address(frame["start"])
            if frame["kind"] not in ("FOR", "DO"):
                raise BasicError("Неизвестный тип цикла в снимке")
            if frame["start"] not in trial.program.pairs:
                raise BasicError("Цикл снимка отсутствует в листинге")
            text = trial.program.instruction(frame["start"]).text.upper()
            if not (
                text.startswith(frame["kind"] + " ")
                or text == frame["kind"]
            ):
                raise BasicError("Некорректный заголовок цикла в снимке")
            if frame["kind"] == "FOR":
                trial.lvalue(frame["name"])
                if not isinstance(frame["step"], (int, float)) or not frame["step"]:
                    raise BasicError("Некорректный STEP в снимке")
                if not isinstance(frame["limit"], (int, float)):
                    raise BasicError("Некорректный предел FOR в снимке")

        trial.data_pointer = int(obj["data_pointer"])
        if not 0 <= trial.data_pointer <= len(trial.program.data):
            raise BasicError("Некорректная позиция DATA")

        state = obj["state"]
        if state not in ("ready", "paused", "ended"):
            raise BasicError("Некорректное состояние снимка")
        trial.state = state

        if state != "ready":
            trial.program.validate()

        trial.steps = int(obj["steps"])
        trial.total_steps = int(obj["total_steps"])
        trial.execution_time = float(obj["elapsed"])
        trial.breakpoints = {int(n) for n in obj["breakpoints"]}
        trial.timers = {
            canonical(name): Timer(value["elapsed"], value["running"])
            for name, value in obj["timers"].items()
        }

        def as_tuple(value):
            return tuple(as_tuple(x) for x in value) if isinstance(value, list) else value

        trial.rng.setstate(as_tuple(obj["random_state"]))
        trial.last_random = obj["last_random"]
        trial.last_exit = obj.get("last_exit", 0)
        trial.last_output = obj.get("last_output", "")

        self.__dict__.update(trial.__dict__)
        self.functions = self.make_functions()

    def load_bas(self, path: str) -> None:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
        nonempty = [line for line in lines if line.strip()]
        numbered = bool(nonempty) and all(
            re.match(r"^\s*\d+(?:\s|$)", line) for line in nonempty
        )

        if numbered:
            source = {}
            for line in nonempty:
                match = re.fullmatch(r"\s*(\d+)\s*(.*)", line)
                number, text = int(match.group(1)), match.group(2)
                if number in source:
                    raise BasicError(f"Повторный номер строки {number}")
                source[number] = text
        else:
            source = {i * 10: line for i, line in enumerate(lines, 1)}

        candidate = Program(source)
        self.close_files()
        self.program = candidate
        self.project_name = Path(path).stem
        self.project_path = ""
        self.variables.clear()
        self.arrays.clear()
        self.calls.clear()
        self.loops.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.pc = candidate.at_or_after((1, 0))
        self.data_pointer = 0
        self.state = "ready"
        self.dirty = False


HELP = {
    "": """
MutaBasic
  HELP commands    Команды оболочки
  HELP language    Операторы BASIC и ограничения
  HELP source      Изменение собственного листинга
  HELP system      Системные показатели и таймеры
  HELP files       Файлы и внешние команды
  HELP snapshots   Проекты, переменные и снимки
  HELP functions   Функции

Номер и текст добавляют/заменяют строку:
  10 PRINT "Hello"
Один номер удаляет строку:
  10

Команды оболочки можно предварять двоеточием:
  :LIST
BASIC принудительно исполняется через:
  BASIC RESTORE
""",
    "commands": """
NEW [имя]                  Новый пустой проект в памяти
LOAD файл.bas              Загрузить листинг
SAVE файл.bas              Сохранить листинг
PROJECT SAVE файл.mbp      Сохранить проект
PROJECT LOAD файл.mbp      Загрузить проект
LIST [от [до]]             Показать листинг
EDIT номер текст           Добавить/заменить строку
INSERT номер текст         Добавить строку; номер должен быть свободен
DELETE от [до]             Удалить строки
FIND текст                 Поиск без учёта регистра
REPLACE "что" "на что"      Замена по всему листингу без учёта регистра
UNDO [n] / REDO [n]         Отмена/повтор изменений листинга
HISTORY                    История изменений
CHECK                      Проверить структуру блоков
RUN [номер|метка]           Новый запуск, переменные очищаются
CONT                       Продолжить
STEP [n]                   Исполнить n инструкций
BREAK [номера]             Добавить/показать точки останова
UNBREAK [номера]           Удалить точки, без аргументов — все
VARS                       Переменные и массивы
SYS                        Системные показатели
SET x=expr                 Присваивание
EVAL expr                  Вычислить выражение
VARSAVE [файл.json]         Сохранить переменные
VARLOAD [файл.json]         Загрузить переменные
SNAPSHOT [файл.json]        Сохранить снимок
RESTORE [файл.json]         Загрузить снимок
PWD / CD каталог           Рабочая папка
FILES [шаблон]             Файлы рабочей папки
SHELL команда              Команда системной оболочки
BASIC инструкция           Принудительно исполнить BASIC
QUIT / EXIT                Выход

NEW/LOAD не спрашивают подтверждение: сохраните изменения заранее.
""",
    "language": """
LET x=expr или x=expr
PRINT expr; expr, expr / ? expr
INPUT ["prompt";] x,y / LINE INPUT ["prompt";] s$
IF expr THEN statement [ELSE statement]
IF expr THEN / ELSE / END IF
FOR i=start TO end [STEP value] / NEXT [i]
WHILE expr / WEND
DO [WHILE|UNTIL expr] / LOOP [WHILE|UNTIL expr]
EXIT FOR / EXIT DO
GOTO target / GOSUB target / RETURN
ON expr GOTO targets / ON expr GOSUB targets
DIM a(10), b(1 TO 5, 2 TO 8)
OPTION BASE 0|1 / ERASE a,b / SWAP a,b
DATA values / READ variables / RESTORE [target]
RANDOMIZE [seed]
STOP / END / SYSTEM / CLS / BEEP / SLEEP [seconds]
REM comment / ' comment
Метки: label: PRINT "hello"

Регистр имён и операторов не важен.
Регистр содержимого строк и путей не изменяется.
Сравнения: -1 = истина, 0 = ложь.
AND OR XOR NOT EQV IMP — побитовые операции, без short circuit.
^ — степень; \\ — целочисленное деление; MOD — остаток.

Нет SUB/FUNCTION/TYPE, SELECT CASE, ELSEIF, ON ERROR,
PRINT USING, графики, двоичных и RANDOM-файлов.
Нет вложенного однострочного IF и NEXT i,j.
Не эмулируются переполнения и точные численные типы QBasic.
Произвольный GOTO через границы активных циклов не очищает их стеки.
""",
    "source": """
SOURCE SET номер, текст$
SOURCE INSERT номер, текст$
SOURCE DELETE от [, до]
SOURCE REPLACE что$, на_что$ [, от, до, учитывать_регистр]
SOURCE UNDO [n]
SOURCE REDO [n]

LINE$(номер)                  Текст строки без номера
LINEEXISTS(номер)             -1 или 0
FINDLINE(текст$ [, от, регистр])  Номер найденной строки или 0
PROGRAM$                      Полный нумерованный листинг

Правила:
  * Каждая SOURCE-инструкция — отдельная транзакция.
  * UNDO/REDO меняют только листинг, не переменные и внешние файлы.
  * При работающей/приостановленной программе блоки должны оставаться целыми.
  * Заголовок активного FOR/DO нельзя удалять или изменять.
  * Тело активного цикла изменять можно.
  * Продолжение идёт с прежнего следующего адреса или ближайшего после него.
  * Вставленные раньше точки продолжения строки задним числом не исполняются.
  * DATA сохраняет порядковый указатель, ограниченный новой длиной DATA.
  * Для предсказуемых мутаций размещайте одну инструкцию на строке.
  * В оболочке при наборе новой программы незакрытые блоки временно допустимы.
""",
    "system": """
Динамические переменные только для чтения:
  VERSION$ PROJECT$ CWD$ STATE$ DATE$ TIME$ TIMER PI TRUE FALSE
  RUNCOUNT LAUNCHCOUNT STEPS TOTALSTEPS UPTIME ELAPSED
  FREEDISK TOTALDISK CURRENTLINE NEXTLINE PID
  CALLDEPTH LOOPDEPTH DATAPOS LINECOUNT HISTORYCOUNT REDOCOUNT
  LASTEXIT LASTOUTPUT$ LASTERROR$ ERRORLINE PROGRAM$

RUNCOUNT — число RUN проекта; хранится в проекте и снимках.
LAUNCHCOUNT — число запусков процесса, хранимое в файле состояния.
STEPS — выполненные инструкции текущего RUN.
TOTALSTEPS — общий счётчик VM, сохраняется в снимке.
ELAPSED — время исполнения VM, включая INPUT/SLEEP, без пауз REPL.
UPTIME — время с создания текущей VM.
FREEDISK/TOTALDISK — байты на диске текущей рабочей папки.
TIMER — секунды с начала текущих суток.
SYS("имя") / SYS$("имя") — системное значение/его строковое представление.
ENVIRON$("NAME") — окружение ОС; регистр ключей зависит от ОС.
ENVIRON "NAME=value" — изменить окружение процесса.

TIMER START "name"
TIMER STOP "name"
TIMER RESET "name"
TIMERGET("name") — секунды именованного таймера.
Именованные таймеры считают реальное монотонное время, включая паузы.
""",
    "files": """
OPEN "file" FOR INPUT|OUTPUT|APPEND AS #n
CLOSE [#n,...]
PRINT #n, expressions / WRITE #n, expressions
INPUT #n, variables / LINE INPUT #n, s$
SEEK #n, position
EOF(n) LOF(n) LOC(n) SEEK(n)

READFILE$("path")
WRITEFILE "path", text$
APPENDFILE "path", text$
COPYFILE "from", "to"
FILEEXISTS("path") DIREXISTS("path") FILESIZE("path")
CHDIR path$ / MKDIR path$ / RMDIR path$ / KILL path$
NAME old$ AS new$

SHELL command$        Выполнить команду оболочки
SHELL(command$)       Код завершения
SHELL$(command$)      Захваченный stdout
LASTEXIT             Последний код завершения
LASTOUTPUT$          Последний захваченный stdout

Для SHELL нужен --allow-shell.
SHELL$ не добавляет stderr к stdout.
Команды запускаются через системную оболочку.

Файлы — UTF-8. INPUT # читает одну CSV-строку за вызов.
PRINT форматируется упрощённо; запятая выводит табуляцию.
SEEK использует позиции текстового потока Python.
Это не произвольный побайтовый доступ QBasic.
MutaBasic не является песочницей.
""",
    "snapshots": """
.bas: текстовый листинг.
.mbp: JSON-проект — листинг, имя, история, счётчик RUN.
variables.json: переменные, массивы, OPTION BASE.
snapshot.json: проект + переменные + позиция исполнения,
стеки GOSUB/циклов, DATA, RNG, таймеры, точки останова.

Оболочка:
  VARSAVE [file] / VARLOAD [file]
  SNAPSHOT [file] / RESTORE [file]

Из программы:
  VARSAVE "file.json"
  VARLOAD "file.json"
  SNAPSHOT "file.json"

Снимок из программы сохраняет продолжение после SNAPSHOT.
Сохранение снимков запрещено при открытых BASIC-файлах.
Снимки не откатывают файлы, окружение ОС и эффекты SHELL.
Промежуток между сохранением и загрузкой не добавляется к таймерам.
Право SHELL не загружается из снимка, его задаёт текущий CLI.
Загружайте проекты и снимки только из доверенных источников.
""",
    "functions": """
ABS ATN COS SIN TAN SQR EXP LOG INT FIX CINT CLNG CSNG CDBL SGN
MIN MAX ROUND FLOOR CEIL RND([n])
LEN ASC CHR$ STR$ VAL HEX$ OCT$
LEFT$ RIGHT$ MID$ LCASE$ UCASE$ LTRIM$ RTRIM$
SPACE$ STRING$ INSTR TAB SPC INKEY$
LBOUND("A" [,dimension]) UBOUND("A" [,dimension])
VAREXISTS("X")
LINE$ LINEEXISTS FINDLINE TIMERGET
SYS SYS$ ENVIRON$
FILEEXISTS DIREXISTS FILESIZE READFILE$ EOF LOF LOC SEEK
SHELL SHELL$

Индексы строк — с 1. Номер измерения массива — с 1.
Неинициализированные переменные: 0; строковые: "".
Массив при первом обращении автоматически создаётся до индекса 10.
TAB/SPC упрощены: возвращают строку пробелов.
""",
}


def path_argument(text: str, default: str | None = None) -> str:
    text = text.strip()
    if not text:
        if default is None:
            raise BasicError("Требуется путь")
        return default
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


class Shell:
    COMMANDS = {
        "HELP", "NEW", "LOAD", "SAVE", "PROJECT", "LIST", "EDIT", "INSERT",
        "DELETE", "FIND", "REPLACE", "UNDO", "REDO", "HISTORY", "CHECK",
        "RUN", "CONT", "STEP", "BREAK", "UNBREAK", "VARS", "SYS", "SET",
        "EVAL", "VARSAVE", "VARLOAD", "SNAPSHOT", "RESTORE", "PWD", "CD",
        "FILES", "SHELL", "BASIC", "QUIT", "EXIT",
    }

    def __init__(self, vm: VM, *, limit=1_000_000, trace=False, quiet=False):
        self.vm = vm
        self.limit = limit
        self.trace = trace
        self.quiet = quiet

    def location(self):
        vm = self.vm
        if vm.pc == ADDRESS_END:
            print(f"[{vm.state}] конец листинга")
        else:
            instruction = vm.program.instruction(vm.program.at_or_after(vm.pc))
            print(f"[{vm.state}] {instruction.address[0]} {instruction.text}")

    def command(self, line: str) -> bool:
        vm = self.vm
        line = line.strip()

        if not line:
            return True

        numbered = re.fullmatch(r"(\d+)(?:\s+(.*))?", line)
        if numbered:
            number = int(numbered.group(1))
            text = numbered.group(2)
            source = dict(vm.program.source)
            if text:
                source[number] = text
            else:
                source.pop(number, None)
            vm.mutate(source, f"edit {number}")
            return True

        if line.startswith(":"):
            line = line[1:].lstrip()

        pieces = line.split(None, 1)
        if not pieces:
            return True

        command = pieces[0].upper()
        argument = pieces[1] if len(pieces) > 1 else ""

        handler = COMMANDS.get(command)
        if handler is not None:
            handler(self, argument)
            return True

        if command not in self.COMMANDS:
            vm.immediate(line)
            return True

        if command in ("QUIT", "EXIT"):
            return False

        if command == "HELP":
            print(HELP.get(argument.lower().strip(), "Неизвестная тема. HELP"))
        elif command == "BASIC":
            vm.immediate(argument)
        elif command == "NEW":
            vm.close_files()
            fresh = VM(
                shell_policy=vm.shell_policy,
                history_limit=vm.history_limit,
                launch_count=vm.launch_count,
            )
            fresh.project_name = path_argument(argument, "untitled")
            self.vm = fresh
        elif command == "LOAD":
            vm.load_bas(path_argument(argument))
        elif command == "SAVE":
            Path(path_argument(argument)).write_text(
                vm.program.listing() + "\n", encoding="utf-8"
            )
        elif command == "PROJECT":
            parts = argument.split(None, 1)
            if len(parts) != 2 or parts[0].upper() not in ("SAVE", "LOAD"):
                raise BasicError("PROJECT SAVE|LOAD файл.mbp")
            method = vm.save_project if parts[0].upper() == "SAVE" else vm.load_project
            method(path_argument(parts[1]))
        elif command == "LIST":
            bounds = [int(x) for x in argument.split()]
            if len(bounds) > 2:
                raise BasicError("LIST [от [до]]")
            low = bounds[0] if bounds else 1
            high = bounds[1] if len(bounds) == 2 else sys.maxsize
            for number, text in vm.program.source.items():
                if low <= number <= high:
                    print(f"{number} {text}")
        elif command in ("EDIT", "INSERT"):
            match = re.fullmatch(r"(\d+)\s+(.*)", argument)
            if not match:
                raise BasicError(f"{command} номер текст")
            number, text = int(match.group(1)), match.group(2)
            source = dict(vm.program.source)
            if command == "INSERT" and number in source:
                raise BasicError("Номер уже занят")
            source[number] = text
            vm.mutate(source, f"{command.lower()} {number}")
        elif command == "DELETE":
            bounds = [int(x) for x in argument.split()]
            if not 1 <= len(bounds) <= 2:
                raise BasicError("DELETE от [до]")
            vm.source_statement("DELETE " + ",".join(map(str, bounds)))
        elif command == "FIND":
            needle = path_argument(argument).casefold()
            for number, text in vm.program.source.items():
                if needle in text.casefold():
                    print(f"{number} {text}")
        elif command == "REPLACE":
            args = shlex.split(argument)
            if len(args) != 2:
                raise BasicError('REPLACE "что" "на что"')
            quoted = ['"' + item.replace('"', '""') + '"' for item in args]
            vm.source_statement("REPLACE " + ",".join(quoted))
        elif command in ("UNDO", "REDO"):
            vm.undo(int(argument or 1), redo=command == "REDO")
        elif command == "HISTORY":
            print(f"Undo: {len(vm.undo_stack)}, redo: {len(vm.redo_stack)}")
            for i, record in enumerate(reversed(vm.undo_stack), 1):
                print(f"{i}: {record.get('time', '')} {record.get('description', '')}")
        elif command == "CHECK":
            vm.program.validate()
            print("Структура блоков корректна; это не полная проверка программы.")
        elif command == "RUN":
            vm.start(argument or None)
            vm.run(limit=self.limit, trace=self.trace)
            if not self.quiet:
                self.location()
        elif command == "CONT":
            vm.run(limit=self.limit, trace=self.trace)
            if not self.quiet:
                self.location()
        elif command == "STEP":
            count = int(argument or 1)
            if count < 1:
                raise BasicError("Количество шагов должно быть положительным")
            vm.run(count=count, limit=self.limit, trace=self.trace)
            if not self.quiet:
                self.location()
        elif command == "BREAK":
            vm.breakpoints.update(int(x) for x in argument.split())
            print("Точки:", *sorted(vm.breakpoints))
        elif command == "UNBREAK":
            if argument:
                vm.breakpoints.difference_update(int(x) for x in argument.split())
            else:
                vm.breakpoints.clear()
        elif command == "VARS":
            for name, value in sorted(vm.variables.items()):
                print(f"{name} = {value!r}")
            for name, array in sorted(vm.arrays.items()):
                print(f"{name}: {array['bounds']}")
                for key, value in sorted(array["values"].items()):
                    print(f"  {name}({key}) = {value!r}")
        elif command == "SYS":
            for name in sorted(vm.readonly - {"PROGRAM$"}):
                print(f"{name} = {vm.system_value(name)}")
        elif command == "SET":
            vm.immediate("LET " + argument)
        elif command == "EVAL":
            print(display(vm.evaluate(argument)))
        elif command in ("VARSAVE", "VARLOAD", "SNAPSHOT", "RESTORE"):
            method, default = {
                "VARSAVE": (vm.save_variables, "variables.json"),
                "VARLOAD": (vm.load_variables, "variables.json"),
                "SNAPSHOT": (vm.save_snapshot, "snapshot.json"),
                "RESTORE": (vm.load_snapshot, "snapshot.json"),
            }[command]
            method(path_argument(argument, default))
        elif command == "PWD":
            print(Path.cwd())
        elif command == "CD":
            os.chdir(path_argument(argument))
        elif command == "FILES":
            pattern = path_argument(argument, "*")
            for path in sorted(Path.cwd().iterdir()):
                if fnmatch.fnmatch(path.name, pattern):
                    print(path.name + ("/" if path.is_dir() else ""))
        elif command == "SHELL":
            vm.shell(argument)

        return True

    def repl(self):
        if not self.quiet:
            print(f"{NAME} {VERSION} · HELP — справка · QUIT — выход")
            print(f"Проект: {self.vm.project_name}")

        while True:
            try:
                if not self.command(input("muta> ")):
                    break
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print("\nQUIT — выход.")
            except Exception as exc:
                print(f"Ошибка: {exc}", file=sys.stderr)


def default_state_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return root / "mutabasic" / "state.json"


def increment_launch_count(path: Path | None) -> int:
    if path is None:
        return 1

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        if path.exists():
            obj = read_json(path, "mutabasic-state")
            count = max(0, int(obj.get("launch_count", 0)))

        count += 1
        atomic_json(path, {
            "format": "mutabasic-state",
            "version": FORMAT_VERSION,
            "launch_count": count,
            "last_launch": dt.datetime.now().astimezone().isoformat(),
        })
        return count
    except Exception as exc:
        print(
            f"Предупреждение: счётчик запусков не сохранён: {exc}",
            file=sys.stderr,
        )
        return 1


def self_test() -> None:
    """Small regression suite; not a full QBasic compatibility suite."""
    checks = 0

    def check(condition, message):
        nonlocal checks
        if not condition:
            raise AssertionError(message)
        checks += 1

    vm = VM()
    check(vm.evaluate("-2^2") == -4, "unary/exponent precedence")
    check(vm.evaluate("2^3^2") == 512, "right-associative exponent")
    check(vm.evaluate("NOT 1 = 1") == 0, "NOT precedence")
    check(vm.evaluate("-7 MOD 3") == -1, "MOD sign")
    check(vm.evaluate('MID$("abcdef",2,3)') == "bcd", "MID$")
    check(vm.evaluate('RIGHT$("abc",0)') == "", "RIGHT$ zero")

    vm.immediate("alpha=5")
    check(vm.evaluate("ALPHA") == 5, "case-insensitive variables")

    vm.immediate("DIM a(1 TO 2, 3 TO 4)")
    vm.immediate("a(2,4)=7")
    check(vm.evaluate("A(2,4)") == 7, "multidimensional array")

    vm.mutate({
        10: "sum=0",
        20: "FOR i=1 TO 5",
        30: "sum=sum+i",
        40: "NEXT i",
        50: "END",
    }, "test")
    vm.start()
    vm.run()
    check(vm.evaluate("SUM") == 15, "FOR execution")

    vm = VM()
    vm.mutate({
        10: 'SOURCE SET 30, "x=42"',
        20: "REM continuation",
        30: "x=0",
        40: "END",
    }, "test")
    vm.start()
    vm.run()
    check(vm.evaluate("x") == 42, "runtime source mutation")
    vm.undo()
    check(vm.program.source[30] == "x=0", "undo source")
    vm.undo()
    check(not vm.program.source, "second undo")
    vm.undo(2, redo=True)
    check(vm.program.source[30] == "x=42", "multi-step redo")

    vm = VM()
    vm.mutate({
        10: "x=0",
        20: "DO",
        30: "x=x+1",
        40: "LOOP UNTIL x=3",
        50: "IF x=3 THEN",
        60: "y=1",
        70: "ELSE",
        80: "y=2",
        90: "END IF",
        100: "END",
    }, "test")
    vm.start()
    vm.run()
    check(vm.evaluate("x") == 3 and vm.evaluate("y") == 1, "DO and block IF")

    vm = VM()
    vm.mutate({
        10: "DATA 3,\"abc\",&H10",
        20: "READ a,b$,c",
        30: "GOSUB subroutine",
        40: "STOP",
        50: "END",
        100: "subroutine: a=a+1",
        110: "RETURN",
    }, "test")
    vm.start()
    vm.run()
    check(vm.evaluate("a") == 4, "DATA/GOSUB")
    check(vm.evaluate("b$") == "abc", "string DATA")
    check(vm.evaluate("c") == 16, "hex DATA")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        vm.save_snapshot(str(root / "snapshot.json"))
        restored = VM()
        restored.load_snapshot(str(root / "snapshot.json"))
        check(restored.evaluate("a") == 4, "snapshot variables")
        check(restored.pc == vm.pc, "snapshot continuation")
        restored.run()
        check(restored.state == "ended", "snapshot resume")

        vm.save_variables(str(root / "variables.json"))
        restored.load_variables(str(root / "variables.json"))
        check(restored.evaluate("b$") == "abc", "variable persistence")

        vm.save_project(str(root / "project.mbp"))
        restored.load_project(str(root / "project.mbp"))
        check(restored.program.source == vm.program.source, "project persistence")

        path = str(root / "data.txt").replace('"', '""')
        vm.immediate(f'OPEN "{path}" FOR OUTPUT AS #1')
        vm.immediate('WRITE #1, 7, "hello,world"')
        vm.immediate("CLOSE #1")
        vm.immediate(f'OPEN "{path}" FOR INPUT AS #1')
        vm.immediate("INPUT #1, n, s$")
        vm.immediate("CLOSE #1")
        check(vm.evaluate("n") == 7, "file numeric input")
        check(vm.evaluate("s$") == "hello,world", "file CSV string")

    check(not vm.allow_shell, "shell denied by default")
    try:
        vm.shell("echo unsafe")
    except BasicError:
        check(True, "shell permission")
    else:
        raise AssertionError("shell permission")

    # Keep the status line ASCII so --self-test works on legacy Windows
    # consoles whose code page cannot represent the Russian UI text.
    print(f"{NAME}: {checks} checks passed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mutabasic",
        description="Консольный BASIC с изменяемым собственным листингом.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("program", nargs="?", help="Файл .bas")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="REPL после выполнения")
    parser.add_argument("--no-run", action="store_true",
                        help="Загрузить без запуска и открыть REPL")
    parser.add_argument("-e", "--execute", action="append", default=[],
                        metavar="COMMAND", help="Команда оболочки; можно повторять")
    parser.add_argument("--project", metavar="FILE", help="Загрузить JSON-проект")
    parser.add_argument("--restore", metavar="FILE", help="Загрузить снимок")
    parser.add_argument("--name", help="Имя проекта")
    parser.add_argument("--cwd", help="Рабочая папка")
    parser.add_argument("--allow-shell", action="store_true",
                        help="Разрешить внешние команды")
    parser.add_argument("--allow-shell-command", action="append", default=[],
                        metavar="COMMAND",
                        help="Разрешить только этот executable после --allow-shell")
    parser.add_argument("--max-steps", type=int, default=1_000_000,
                        help="Лимит инструкций на RUN/CONT; 0 — без лимита")
    parser.add_argument("--history-limit", type=int, default=100,
                        help="Максимальное число изменений undo/redo")
    parser.add_argument("--trace", action="store_true",
                        help="Трассировка инструкций в stderr")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Без приветствия и сообщений RUN")
    parser.add_argument("--check", action="store_true",
                        help="Проверить структуру блоков без исполнения")
    parser.add_argument("--state-file", default=str(default_state_path()),
                        help="JSON-файл постоянного счётчика запусков")
    parser.add_argument("--no-state", action="store_true",
                        help="Не читать и не записывать постоянный счётчик")
    parser.add_argument("--project-out", metavar="FILE",
                        help="Сохранить проект перед выходом")
    parser.add_argument("--vars-out", metavar="FILE",
                        help="Сохранить переменные перед выходом")
    parser.add_argument("--snapshot-out", metavar="FILE",
                        help="Сохранить снимок перед выходом")
    parser.add_argument("--self-test", action="store_true",
                        help="Запустить встроенные smoke-тесты")
    parser.add_argument("--version", action="version",
                        version=f"{NAME} {VERSION}")
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args()

    if args.max_steps < 0:
        parser.error("--max-steps должен быть >= 0")
    if args.history_limit < 1:
        parser.error("--history-limit должен быть >= 1")
    if sum(bool(x) for x in (args.program, args.project, args.restore)) > 1:
        parser.error("Укажите только один источник: program, --project или --restore")

    if args.self_test:
        try:
            self_test()
            return 0
        except Exception as exc:
            print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
            return 1

    shell = None
    status = 0

    try:
        if args.cwd:
            os.chdir(args.cwd)

        launch_count = increment_launch_count(
            None if args.no_state else Path(args.state_file)
        )

        vm = VM(
            shell_policy=ShellPolicy(
                enabled=args.allow_shell,
                commands=frozenset(args.allow_shell_command),
            ),
            history_limit=args.history_limit,
            launch_count=launch_count,
        )

        if args.program:
            vm.load_bas(args.program)
        elif args.project:
            vm.load_project(args.project)
        elif args.restore:
            vm.load_snapshot(args.restore)

        if args.name:
            vm.project_name = args.name

        shell = Shell(
            vm,
            limit=args.max_steps,
            trace=args.trace,
            quiet=args.quiet,
        )

        if args.check:
            vm.program.validate()
            if not args.quiet:
                print("Структура блоков корректна.")
            return 0

        has_input = bool(args.program or args.project or args.restore)

        if has_input and not args.no_run:
            if not args.restore:
                vm.start()
            vm.run(limit=args.max_steps, trace=args.trace)

        keep_going = True
        for command in args.execute:
            keep_going = shell.command(command)
            if not keep_going:
                break

        interactive = (
            args.interactive
            or args.no_run
            or (not has_input and not args.execute)
        )
        if interactive and keep_going:
            shell.repl()

    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        status = 130
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        status = 1
    finally:
        if shell is not None:
            vm = shell.vm
            try:
                if args.project_out:
                    vm.save_project(args.project_out)
                if args.vars_out:
                    vm.save_variables(args.vars_out)
                if args.snapshot_out:
                    vm.save_snapshot(args.snapshot_out)
            except Exception as exc:
                print(f"Ошибка сохранения: {exc}", file=sys.stderr)
                status = 1
            finally:
                vm.close_files()

    return status


if __name__ == "__main__":
    raise SystemExit(main())

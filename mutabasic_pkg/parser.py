"""Parsing facade with token, AST, and source-location metadata."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .core import parse_source as _parse_source
from .core import split_statements as _split_statements
from .core import tokenize as _tokenize
from .core import TOKEN as _TOKEN

__all__ = [
    "ASTNode",
    "ProgramAST",
    "SourceLocation",
    "Token",
    "lex",
    "parse_program",
    "parse_source",
    "split_statements",
    "tokenize",
]


@dataclass(frozen=True)
class SourceLocation:
    file: str | None = None
    start: int = 0
    end: int = 0
    line: int = 1
    column: int = 1


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    location: SourceLocation


@dataclass(frozen=True)
class ASTNode:
    kind: str
    value: Any = None
    location: SourceLocation | None = None
    children: tuple["ASTNode", ...] = ()


@dataclass(frozen=True)
class ProgramAST:
    source: dict[int, str]
    statements: tuple[ASTNode, ...] = ()
    tokens: tuple[Token, ...] = ()
    locations: dict[int, SourceLocation] = field(default_factory=dict)


def parse_source(source: str) -> dict[int, str]:
    return _parse_source(source)


def split_statements(text: str) -> list[str]:
    return _split_statements(text)


def tokenize(text: str) -> list[tuple[str, str]]:
    return _tokenize(text)


def lex(text: str) -> list[Token]:
    tokens: list[Token] = []
    position = 0
    line = 1
    column = 1
    while position < len(text):
        if not text[position:].strip():
            break
        match = _TOKEN.match(text, position)
        if not match:
            raise ValueError(f"Недопустимая часть выражения: {text[position:]!r}")
        kind = match.lastgroup
        value = match.group(kind)
        start = position
        end = match.end()
        token = Token(
            kind=kind,
            value=value,
            location=SourceLocation(start=start, end=end, line=line, column=column),
        )
        tokens.append(token)
        position = end
        for char in value:
            if char == "\n":
                line += 1
                column = 1
            else:
                column += 1
    tokens.append(Token("end", "", SourceLocation(start=position, end=position, line=line, column=column)))
    return tokens


def parse_program(source: str | dict[int, str]) -> ProgramAST:
    if isinstance(source, str):
        lines = parse_source(source)
    else:
        lines = {int(k): str(v) for k, v in source.items()}
    statements: list[ASTNode] = []
    tokens: list[Token] = []
    locations: dict[int, SourceLocation] = {}
    for number, text in sorted(lines.items()):
        location = SourceLocation(start=0, end=len(text), line=number, column=1)
        locations[number] = location
        statements.append(ASTNode("statement", text, location, ()))
        tokens.extend(lex(text))
    return ProgramAST(source=lines, statements=tuple(statements), tokens=tuple(tokens), locations=locations)

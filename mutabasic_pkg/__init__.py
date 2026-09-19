"""Public package for embedding and extending MutaBasic."""

from .api import MutaBasic, create_vm
from .core import (
    ADDRESS_END,
    BasicError,
    Expression,
    Instruction,
    Program,
    Shell,
    VM,
    VERSION,
    build_parser,
    main,
    self_test,
)
from .parser import parse_source, split_statements, tokenize
from .registry import register_command, register_function
from .security import ShellPolicy

__all__ = [
    "ADDRESS_END", "BasicError", "Expression", "Instruction", "Program",
    "Shell", "ShellPolicy", "VM", "VERSION", "MutaBasic", "create_vm",
    "build_parser", "main", "self_test", "parse_source", "split_statements",
    "tokenize", "register_command", "register_function",
]

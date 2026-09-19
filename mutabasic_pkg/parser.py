"""Parsing facade.

The interpreter currently uses a pragmatic statement parser rather than a
full grammar.  This module is the stable seam for a future AST parser.
"""
from .core import parse_source, split_statements, tokenize

__all__ = ["parse_source", "split_statements", "tokenize"]

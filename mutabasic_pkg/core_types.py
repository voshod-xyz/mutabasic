"""Small shared types used without importing the interpreter core."""


class BasicPolicyError(Exception):
    """Internal policy error, converted to BasicError by the core."""

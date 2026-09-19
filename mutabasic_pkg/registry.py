"""Extension registries for embedded applications."""

FUNCTIONS: dict[str, object] = {}
COMMANDS: dict[str, object] = {}


def register_function(name: str, function):
    """Register a BASIC function for subsequently created VMs."""
    FUNCTIONS[name.upper()] = function
    return function


def register_command(name: str, handler):
    """Register a Shell command handler ``handler(shell, argument)``."""
    COMMANDS[name.upper()] = handler
    return handler

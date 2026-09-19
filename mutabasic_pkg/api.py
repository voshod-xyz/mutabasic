"""Small, stable embedded Python API."""
from .core import VM
from .security import ShellPolicy


def create_vm(*, allow_shell=False, shell_policy=None, **kwargs) -> VM:
    """Create a VM without relying on CLI globals."""
    if shell_policy is None:
        shell_policy = ShellPolicy(enabled=allow_shell)
    return VM(shell_policy=shell_policy, **kwargs)


class MutaBasic:
    """Convenience facade for applications embedding the interpreter."""

    def __init__(self, **kwargs):
        self.vm = create_vm(**kwargs)

    def load(self, source: dict[int, str]):
        self.vm.mutate(source, "api load")
        return self

    def run(self, start=None, **kwargs):
        self.vm.start(start)
        self.vm.run(**kwargs)
        return self

    def evaluate(self, expression):
        return self.vm.evaluate(expression)

    def execute(self, statement):
        return self.vm.immediate(statement)

    def close(self):
        self.vm.close_files()

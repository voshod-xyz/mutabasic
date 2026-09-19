import unittest
import os
import subprocess
import sys

from mutabasic_pkg import (
    BasicError, MutaBasic, ShellPolicy, VM, parse_source, register_command,
    register_function,
)


class MutaBasicTests(unittest.TestCase):
    def test_cli_help_supports_non_utf8_console(self):
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp1252"
        result = subprocess.run(
            [sys.executable, "mutabasic.py", "--help"],
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_expression_and_program_execution(self):
        app = MutaBasic().load({10: "x=2^3", 20: "END"}).run()
        self.assertEqual(app.evaluate("x"), 8)

    def test_parser_facade(self):
        self.assertEqual(parse_source('10 PRINT "a"\n20 END'), {
            10: 'PRINT "a"', 20: "END"
        })
        with self.assertRaises(BasicError):
            parse_source("10 PRINT 1\n10 END")
        with self.assertRaises(BasicError):
            parse_source("0 END")

    def test_embedded_execution(self):
        vm = VM()
        vm.immediate("a=4")
        self.assertEqual(vm.evaluate("A+1"), 5)

    def test_function_registry(self):
        register_function("TRIPLE", lambda value: value * 3)
        self.assertEqual(VM().evaluate("TRIPLE(7)"), 21)

    def test_command_registry(self):
        seen = []
        register_command("MARK", lambda shell, argument: seen.append(argument))
        shell = __import__("mutabasic_pkg").Shell(VM())
        shell.command("MARK ready")
        self.assertEqual(seen, ["ready"])

    def test_shell_policy_is_explicit_and_restrictable(self):
        with self.assertRaises(BasicError):
            VM().shell("echo denied")
        vm = VM(shell_policy=ShellPolicy(enabled=True, commands=frozenset({"echo"})))
        self.assertEqual(vm.shell("echo allowed", capture=True).strip(), "allowed")
        with self.assertRaises(BasicError):
            vm.shell("python -c \"print(1)\"")
        with self.assertRaises(BasicError):
            vm.shell("echo allowed & python -c \"print(1)\"")


if __name__ == "__main__":
    unittest.main()

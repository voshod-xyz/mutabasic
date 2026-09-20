import unittest
import contextlib
import io
import os
import subprocess
import sys

from mutabasic_pkg import (
    BasicError, FilePolicy, MutaBasic, ShellPolicy, VM, lex, parse_program,
    parse_source, register_command, register_function,
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
        self.assertIn("Примеры:", result.stdout)
        self.assertIn("Темы REPL:", result.stdout)
        self.assertIn("--self-test", result.stdout)

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

    def test_builtin_help_documents_shell_policy_and_line_numbers(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            __import__("mutabasic_pkg").Shell(VM()).command("HELP files")
            __import__("mutabasic_pkg").Shell(VM()).command("HELP source")
        text = output.getvalue()
        self.assertIn("--allow-shell-command", text)
        self.assertIn("Цепочки", text)
        self.assertIn("повторные номера запрещены", text)

    def test_shell_policy_is_explicit_and_restrictable(self):
        with self.assertRaises(BasicError):
            VM().shell("echo denied")
        vm = VM(shell_policy=ShellPolicy(enabled=True, commands=frozenset({"echo"})))
        self.assertEqual(vm.shell("echo allowed", capture=True).strip(), "allowed")
        with self.assertRaises(BasicError):
            vm.shell("python -c \"print(1)\"")
        with self.assertRaises(BasicError):
            vm.shell("echo allowed & python -c \"print(1)\"")

    def test_parser_metadata_and_checkpoint(self):
        tokens = lex("x=1")
        self.assertEqual(tokens[0].kind, "name")
        self.assertEqual(parse_program("10 END").source, {10: "END"})

        vm = VM(seed=7, fs_policy=FilePolicy(roots=(os.getcwd(),)))
        vm.immediate("x=1")
        vm.checkpoint("before")
        vm.immediate("x=2")
        vm.rollback("before")
        self.assertEqual(vm.evaluate("x"), 1)

    def test_listing_edit_counter_is_readonly_and_persisted(self):
        vm = VM()
        self.assertEqual(vm.evaluate("LISTINGEDITS"), 0)
        vm.mutate({10: "x=1"}, "test edit")
        self.assertEqual(vm.evaluate("LISTINGEDITS"), 1)
        vm.mutate({10: "x=2"}, "test edit")
        self.assertEqual(vm.evaluate("LISTINGEDITS"), 2)
        vm.undo()
        self.assertEqual(vm.evaluate("LISTINGEDITS"), 3)
        vm.undo(redo=True)
        self.assertEqual(vm.evaluate("LISTINGEDITS"), 4)

    def test_seeded_randomize_and_nested_loops(self):
        source = {
            10: "RANDOMIZE 42",
            20: "a=RND",
            30: "x=0",
            40: "FOR i=1 TO 2",
            50: "FOR j=1 TO 2",
            60: "x=x+1",
            70: "NEXT j",
            80: "NEXT i",
            90: "END",
        }
        first = MutaBasic().load(source).run()
        second = MutaBasic().load(source).run()
        self.assertEqual(first.evaluate("a"), second.evaluate("a"))
        self.assertEqual(first.evaluate("x"), 4)

    def test_recursive_function_and_text_helpers(self):
        app = MutaBasic().load({
            10: "FUNCTION FACT(n)",
            20: "IF n<=1 THEN FACT=1:END FUNCTION",
            30: "FACT=n*FACT(n-1)",
            40: "END FUNCTION",
            50: "x=FACT(5)",
            60: "s$=REPLACE$(\"Hello hello\", \"hello\", \"X\")",
            70: "END",
        }).run()
        self.assertEqual(app.evaluate("x"), 120)
        self.assertEqual(app.evaluate("s$"), "X X")

    def test_procedure_parameters_are_recursion_safe_and_local(self):
        app = MutaBasic().load({
            10: "FUNCTION F(n)",
            20: "IF n<=0 THEN F=0:END FUNCTION",
            30: "F=n+F(n-1)",
            40: "END FUNCTION",
            50: "x=F(4)",
            60: "END",
        }).run()
        self.assertEqual(app.evaluate("x"), 10)
        self.assertEqual(app.evaluate("n"), 0)

    def test_source_transaction_is_atomic(self):
        vm = VM()
        vm.mutate({10: "x=1", 20: "END"}, "setup")
        vm.immediate('SOURCE BEGIN')
        vm.immediate('SOURCE SET 10, "x=2"')
        self.assertEqual(vm.program.source[10], "x=1")
        vm.immediate("SOURCE COMMIT")
        self.assertEqual(vm.program.source[10], "x=2")
        vm.immediate("SOURCE BEGIN")
        with self.assertRaises(BasicError):
            vm.immediate('SOURCE SET 0, "invalid"')
        self.assertNotIn(0, vm.program.source)
        vm.immediate("SOURCE ROLLBACK")

    def test_listing_versions_and_text_functions(self):
        vm = VM()
        self.assertEqual(vm.evaluate("DIRTY"), 0)
        vm.mutate({10: "END"}, "setup")
        self.assertEqual(vm.evaluate("LISTINGVERSION"), 1)
        self.assertEqual(vm.evaluate("DIRTY"), -1)
        vm.immediate('x$=" hello "')
        self.assertEqual(vm.evaluate('TRIM$(x$)'), "hello")
        self.assertEqual(vm.evaluate('JOIN$("-", "a", "b")'), "a-b")
        self.assertEqual(vm.evaluate('SPLITCOUNT("a,b,c")'), 3)
        self.assertEqual(vm.evaluate('CSVESCAPE$("a""b")'), '"a""b"')


if __name__ == "__main__":
    unittest.main()

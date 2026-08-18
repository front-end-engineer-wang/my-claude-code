import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from coding_assistant import hooks


def bash_block(command: str):
    return SimpleNamespace(name="bash", input={"command": command})


def mcp_block(name: str):
    return SimpleNamespace(name=name, input={})


class BashPermissionTests(unittest.TestCase):
    def test_request_mode_asks_for_ordinary_command(self):
        with (
            patch.object(hooks, "PERMISSION_MODE", "request"),
            patch.object(hooks.CONSOLE, "ask", return_value="yes") as ask,
            patch.object(hooks, "terminal_print"),
        ):
            self.assertIsNone(hooks.permission_hook(bash_block("python -m unittest")))
        ask.assert_called_once()

    def test_full_mode_auto_approves_ordinary_command(self):
        with (
            patch.object(hooks, "PERMISSION_MODE", "full"),
            patch.object(hooks.CONSOLE, "ask", side_effect=AssertionError("prompted")),
            patch.object(hooks, "terminal_print") as output,
        ):
            self.assertIsNone(hooks.permission_hook(bash_block("git status")))
        output.assert_called_once()

    def test_full_mode_requires_confirmation_for_risky_commands(self):
        commands = [
            "Remove-Item -Recurse build",
            "rm -rf build",
            "git reset --hard HEAD~1",
            "git clean -fd",
            "cd .. && npm test",
            "Set-Location '..' ; npm test",
            "echo secret > C:\\outside.txt",
            "powershell -EncodedCommand ZQBjAGgAbwA=",
        ]
        for command in commands:
            with self.subTest(command=command):
                action, reason = hooks.classify_bash_command(command, mode="full")
                self.assertEqual(action, "confirm")
                self.assertIsNotNone(reason)

    def test_full_mode_risky_command_uses_console_decision(self):
        block = bash_block("git reset --hard HEAD~1")
        with (
            patch.object(hooks, "PERMISSION_MODE", "full"),
            patch.object(hooks.CONSOLE, "ask", return_value="yes") as ask,
            patch.object(hooks, "terminal_print"),
        ):
            self.assertIsNone(hooks.permission_hook(block))
        ask.assert_called_once()
        with (
            patch.object(hooks, "PERMISSION_MODE", "full"),
            patch.object(hooks.CONSOLE, "ask", return_value="no"),
            patch.object(hooks, "terminal_print"),
        ):
            self.assertEqual(
                hooks.permission_hook(block), "Permission denied by user"
            )

    def test_catastrophic_commands_are_blocked_in_every_mode(self):
        for mode in ("request", "full"):
            with self.subTest(mode=mode):
                action, reason = hooks.classify_bash_command(
                    "shutdown /s", mode=mode
                )
                self.assertEqual(action, "deny")
                self.assertIn("power", reason)

    def test_async_request_denies_but_full_allows(self):
        results = {}

        def run(mode: str):
            with (
                patch.object(hooks, "PERMISSION_MODE", mode),
                patch.object(hooks, "terminal_print"),
            ):
                results[mode] = hooks.permission_hook(bash_block("python -V"))

        request_thread = threading.Thread(target=run, args=("request",))
        request_thread.start()
        request_thread.join()
        full_thread = threading.Thread(target=run, args=("full",))
        full_thread.start()
        full_thread.join()
        self.assertIn("interactive shell approval", results["request"])
        self.assertIsNone(results["full"])

    def test_async_full_risky_command_fails_closed(self):
        result = []

        def run():
            with (
                patch.object(hooks, "PERMISSION_MODE", "full"),
                patch.object(hooks, "terminal_print"),
            ):
                result.append(
                    hooks.permission_hook(bash_block("Remove-Item build"))
                )

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        self.assertIn("interactive shell approval", result[0])


class McpPermissionTests(unittest.TestCase):
    def setUp(self):
        hooks.mcp_tool_policies = {
            "mcp__docs__search": "allow",
            "mcp__deploy__trigger": "confirm",
            "mcp__admin__drop": "deny",
        }

    def test_full_mode_skips_confirm_but_never_skips_deny(self):
        with (
            patch.object(hooks, "PERMISSION_MODE", "full"),
            patch.object(hooks.CONSOLE, "ask", side_effect=AssertionError("prompted")),
            patch.object(hooks, "terminal_print"),
        ):
            self.assertIsNone(
                hooks.permission_hook(mcp_block("mcp__deploy__trigger"))
            )
            denial = hooks.permission_hook(mcp_block("mcp__admin__drop"))
        self.assertIn("host policy", denial)


if __name__ == "__main__":
    unittest.main()

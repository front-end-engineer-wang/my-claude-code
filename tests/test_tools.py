import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from coding_assistant import tools
from coding_assistant.registry import assemble_tool_pool


class SearchTextTests(unittest.TestCase):
    def test_rg_is_preferred_when_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(tools.shutil, "which", return_value="rg"),
                patch.object(
                    tools, "_search_with_rg",
                    return_value=(["sample.py:1:1:needle"], False),
                ) as rg_search,
            ):
                result = tools.run_search_text("needle", cwd=root)
            rg_search.assert_called_once()
            self.assertIn("Search backend: rg", result)

    def test_python_fallback_honors_glob_case_and_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.py").write_text("Needle\nneedle\n", encoding="utf-8")
            (root / "two.txt").write_text("NEEDLE\n", encoding="utf-8")
            with patch.object(tools.shutil, "which", return_value=None):
                result = tools.run_search_text(
                    "needle", glob="*.py", case_sensitive=False,
                    max_results=1, cwd=root
                )
            self.assertIn("Search backend: python", result)
            self.assertIn("limit reached: 1", result)
            self.assertIn("one.py:1:1:Needle", result)
            self.assertNotIn("two.txt", result)


class ApplyPatchTests(unittest.TestCase):
    def test_multiple_files_and_multiple_hunks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("alpha\nbeta\n", encoding="utf-8")
            second.write_text("one\ntwo\n", encoding="utf-8")
            result = tools.run_apply_patch([
                {"path": "first.py", "hunks": [
                    {"old_text": "alpha", "new_text": "ALPHA"},
                    {"old_text": "beta", "new_text": "BETA"},
                ]},
                {"path": "second.py", "hunks": [
                    {"old_text": "one", "new_text": "ONE"},
                ]},
            ], cwd=root)
            self.assertIn("Patched 2 file(s), 3 hunk(s)", result)
            self.assertEqual(first.read_text(encoding="utf-8"), "ALPHA\nBETA\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "ONE\ntwo\n")

    def test_context_failure_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("before\n", encoding="utf-8")
            second.write_text("unchanged\n", encoding="utf-8")
            result = tools.run_apply_patch([
                {"path": "first.py", "hunks": [
                    {"old_text": "before", "new_text": "after"},
                ]},
                {"path": "second.py", "hunks": [
                    {"old_text": "missing", "new_text": "new"},
                ]},
            ], cwd=root)
            self.assertIn("context mismatch", result)
            self.assertEqual(first.read_text(encoding="utf-8"), "before\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "unchanged\n")

    def test_sha_and_workspace_boundary_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.py"
            target.write_text("old\n", encoding="utf-8")
            wrong_sha = hashlib.sha256(b"different").hexdigest()
            result = tools.run_apply_patch([
                {"path": "target.py", "expected_sha256": wrong_sha,
                 "hunks": [{"old_text": "old", "new_text": "new"}]},
            ], cwd=root)
            self.assertIn("stale file", result)
            self.assertIn("old", target.read_text(encoding="utf-8"))
            result = tools.run_apply_patch([
                {"path": "../outside.py", "hunks": [
                    {"old_text": "old", "new_text": "new"},
                ]},
            ], cwd=root)
            self.assertIn("Path escapes workspace", result)


class RegistryTests(unittest.TestCase):
    def test_new_tools_are_registered(self):
        schemas, handlers = assemble_tool_pool()
        names = {schema["name"] for schema in schemas}
        self.assertIn("search_text", names)
        self.assertIn("apply_patch", names)
        self.assertIn("search_text", handlers)
        self.assertIn("apply_patch", handlers)


if __name__ == "__main__":
    unittest.main()

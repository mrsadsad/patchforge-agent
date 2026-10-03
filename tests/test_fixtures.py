"""Validate the synthetic development fixtures independently of the agent runner."""

import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TASKS = Path(__file__).resolve().parents[1] / "tasks"


def fixtures():
    return [task for path in sorted(TASKS.glob("*.json"))
            if (task := json.loads(path.read_text(encoding="utf-8"))).get('split') == 'development']


def execute_fixture(task, patched=False, include_hidden=False):
    """Use a new directory/process so imports and bytecode never cross cases."""
    files = dict(task["files"])
    if patched:
        for edit in task["demo_edits"]:
            source = files[edit["path"]]
            if source.count(edit["old"]) != 1:
                raise AssertionError(f"{task['id']}: reference edit must match exactly once")
            files[edit["path"]] = source.replace(edit["old"], edit["new"], 1)
    if include_hidden:
        files["test_hidden.py"] = task["hidden_tests"]
    with tempfile.TemporaryDirectory(prefix="patchforge-fixture-") as directory:
        root = Path(directory)
        for filename, source in files.items():
            (root / filename).write_text(source, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py", "-v"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
        )


class FixtureIntegrityTests(unittest.TestCase):
    def test_schema_and_test_coverage(self):
        tasks = fixtures()
        self.assertEqual(len(tasks), 8)
        self.assertEqual(len({task["id"] for task in tasks}), len(tasks))
        required = {"id", "title", "category", "difficulty", "split", "issue", "files", "editable", "hidden_tests", "demo_edits"}
        for task in tasks:
            with self.subTest(task=task["id"]):
                self.assertTrue(required.issubset(task))
                self.assertEqual(task["split"], "development")
                self.assertIn(task["difficulty"], {"easy", "medium"})
                self.assertIn("synthetic", task["issue"].lower())
                self.assertEqual(task["editable"], ["solution.py"])
                self.assertEqual(set(task["files"]), {"solution.py", "test_visible.py"})
                self.assertTrue(task["demo_edits"])
                for edit in task["demo_edits"]:
                    self.assertEqual(edit["path"], "solution.py")
                tests = [ast.parse(task["files"]["test_visible.py"]), ast.parse(task["hidden_tests"])]
                counts = [sum(isinstance(node, ast.FunctionDef) and node.name.startswith("test_") for node in ast.walk(tree)) for tree in tests]
                self.assertGreaterEqual(counts[0], 2)
                self.assertGreaterEqual(counts[1], 2)
                self.assertGreaterEqual(sum(counts), 5)
                self.assertLessEqual(sum(counts), 8)

    def test_every_baseline_fails_a_visible_assertion(self):
        for task in fixtures():
            with self.subTest(task=task["id"]):
                result = execute_fixture(task)
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn("FAIL:", output)
                self.assertNotIn("ERROR:", output)

    def test_reference_edits_pass_visible_and_hidden(self):
        for task in fixtures():
            with self.subTest(task=task["id"]):
                result = execute_fixture(task, patched=True, include_hidden=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

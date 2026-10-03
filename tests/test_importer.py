import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from forge.importer import ImportTaskError, import_task
from forge.repository import Repository, load_tasks


TEST = "import unittest\nfrom package.calc import add\nclass Checks(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
HIDDEN = "import unittest\nfrom package.calc import add\nclass Withheld(unittest.TestCase):\n    def test_negative(self):\n        self.assertEqual(add(-2, 1), -1)\n"


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="patchforge-import-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.output = self.root / "tasks"
        self.issue = self.root / "issue.txt"
        self.issue.write_text("Fix addition for negative numbers.\n", encoding="utf-8")
        self.write("package/__init__.py", "")
        self.write("package/calc.py", "def add(a, b):\n    return a - b\n")
        self.write("tests/test_public.py", TEST)
        self.write("private/test_withheld.py", HIDDEN)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_import(self, **changes):
        arguments = dict(repo=self.repo, issue_file=self.issue, editable=["package/calc.py"],
                         visible_tests=["tests/test_public.py"], hidden_tests=["private/test_withheld.py"],
                         task_id="local-addition", title="真实仓库加法缺陷", task_dir=self.output)
        arguments.update(changes)
        return import_task(**arguments)

    def test_import_preserves_sources_flattens_visible_and_withholds_hidden(self):
        self.write("README.md", "# Example package\n")
        self.write("package/README.md", "Nested documentation is not selected.\n")
        self.write("tests/test_unselected.py", "raise RuntimeError('not selected')\n")
        self.write("root_test.py", "raise RuntimeError('not selected')\n")
        output = self.run_import()
        task = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(set(task["files"]), {"package/__init__.py", "package/calc.py", "test_public.py", "README.md"})
        self.assertEqual(task["files"]["test_public.py"], TEST)
        self.assertEqual(task["hidden_tests"], HIDDEN)
        self.assertNotIn("class Withheld", "\n".join(task["files"].values()))
        self.assertEqual(task["editable"], ["package/calc.py"])
        self.assertEqual(task["split"], "imported")
        self.assertFalse(task["demo_available"])
        self.assertEqual(task["demo_edits"], [])
        self.assertEqual(task["provenance"]["file_count"], 5)
        loaded = load_tasks(self.output)["local-addition"]
        workspace = Repository(self.root / "workspace", loaded)
        self.assertEqual(workspace.read("package/calc.py"), "def add(a, b):\n    return a - b\n")
        self.assertFalse((workspace.root / "private/test_withheld.py").exists())

    def test_skips_sensitive_names_environment_build_and_secret_content(self):
        excluded = [".env", ".env.py", ".git/hooks/pre_commit.py", "venv/site.py", "env/site.py",
                    "__pycache__/cache.py", "build/generated.py", "dist/generated.py",
                    "secrets.py", "credentials.py", "node_modules/helper.py"]
        for name in excluded:
            self.write(name, "PRIVATE_DATA = 123\n")
        self.write("config.py", 'api_key = "this-is-a-secret-value"\n')
        self.write("key.py", 'PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----"\n')
        task = json.loads(self.run_import().read_text(encoding="utf-8"))
        self.assertFalse(set(excluded) & set(task["files"]))
        self.assertNotIn("config.py", task["files"])
        self.assertNotIn("key.py", task["files"])
        self.assertNotIn("PRIVATE_DATA", json.dumps(task))

    def test_selected_sensitive_source_and_issue_are_rejected(self):
        self.write("package/calc.py", 'password = "do-not-import-this"\n')
        with self.assertRaisesRegex(ImportTaskError, "secret_content_not_allowed"):
            self.run_import()
        self.write("package/calc.py", "def add(a, b): return a-b\n")
        self.issue.write_text('access_token = "do-not-import-this"', encoding="utf-8")
        with self.assertRaisesRegex(ImportTaskError, "secret_content_not_allowed"):
            self.run_import()
        self.assertFalse(self.output.exists())

    def test_rejects_unsafe_relative_paths_and_invalid_slug(self):
        for name in ("../outside.py", "/outside.py", "C:/outside.py", "package\\calc.py", "./package/calc.py", "package//calc.py", "package/calc.py ", ".git/file.py"):
            with self.subTest(name=name), self.assertRaises(ImportTaskError):
                self.run_import(editable=[name])
        for slug in ("../escape", "UpperCase", "two--dashes", "has_underscore", "a" * 65, ""):
            with self.subTest(slug=slug), self.assertRaisesRegex(ImportTaskError, "invalid_task_id"):
                self.run_import(task_id=slug)
        self.assertFalse(self.output.exists())

    def test_requires_explicit_distinct_source_and_test_selection(self):
        cases = [dict(editable=[]), dict(visible_tests=[]), dict(hidden_tests=[]),
                 dict(hidden_tests=["private/test_withheld.py", "tests/test_public.py"]),
                 dict(editable=["tests/test_public.py"]), dict(editable=["missing.py"]),
                 dict(editable=["package/calc.py", "package/calc.py"]),
                 dict(hidden_tests=["tests/test_public.py"])]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ImportTaskError):
                self.run_import(**changes)

    def test_basename_collision_and_reserved_hidden_name(self):
        self.write("other/test_public.py", TEST)
        with self.assertRaisesRegex(ImportTaskError, "test_basename_collision"):
            self.run_import(visible_tests=["tests/test_public.py", "other/test_public.py"])
        with self.assertRaisesRegex(ImportTaskError, "test_basename_collision"):
            self.run_import(hidden_tests=["other/test_public.py"])
        self.write("other/test_hidden.py", TEST)
        with self.assertRaisesRegex(ImportTaskError, "test_basename_collision"):
            self.run_import(visible_tests=["other/test_hidden.py"])

    def test_file_and_issue_size_caps(self):
        self.write("oversized.py", "#" * 40_001)
        with self.assertRaisesRegex(ImportTaskError, "file_size_limit"):
            self.run_import()
        (self.repo / "oversized.py").unlink()
        self.issue.write_text("x" * 8_001, encoding="utf-8")
        with self.assertRaisesRegex(ImportTaskError, "issue_size_limit"):
            self.run_import()
        self.assertFalse(self.output.exists())

    def test_total_size_cap(self):
        for index in range(8):
            self.write(f"module_{index}.py", "#" * 39_000)
        with self.assertRaisesRegex(ImportTaskError, "total_size_limit"):
            self.run_import()
        self.assertFalse(self.output.exists())

    def test_file_count_cap_includes_hidden_and_visible(self):
        for index in range(57):
            self.write(f"module_{index}.py", "# fixture\n")
        with self.assertRaisesRegex(ImportTaskError, "file_count_limit"):
            self.run_import()
        self.assertFalse(self.output.exists())

    def test_existing_task_never_overwritten(self):
        output = self.run_import()
        before = output.read_bytes()
        with self.assertRaisesRegex(ImportTaskError, "task_already_exists"):
            self.run_import(title="Replacement title")
        self.assertEqual(output.read_bytes(), before)

    def test_import_never_executes_python(self):
        sentinel = self.root / "executed.txt"
        self.write("module.py", f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n")
        self.run_import()
        self.assertFalse(sentinel.exists())

    def test_utf8_plain_text_required(self):
        self.issue.write_bytes(b"issue\x00binary")
        with self.assertRaisesRegex(ImportTaskError, "plain_text_required"):
            self.run_import()
        self.issue.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(ImportTaskError, "utf8_text_required"):
            self.run_import()

    def test_symlinks_skipped_and_explicit_selection_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "leak.py").write_text("outside = True\n", encoding="utf-8")
        link = self.repo / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != "nt":
                self.skipTest("This host cannot create symlinks")
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.skipTest("This host cannot create symlinks or junctions")
        task = json.loads(self.run_import().read_text(encoding="utf-8"))
        self.assertNotIn("linked/leak.py", task["files"])
        with self.assertRaisesRegex(ImportTaskError, "links_not_allowed"):
            self.run_import(editable=["linked/leak.py"], task_id="linked-import")
        with self.assertRaisesRegex(ImportTaskError, "links_not_allowed"):
            self.run_import(repo=link, task_id="linked-root")

    def test_hidden_hardlink_cannot_leak_into_snapshot(self):
        alias = self.repo / "hidden_alias.py"
        try:
            os.link(self.repo / "private/test_withheld.py", alias)
        except (OSError, NotImplementedError):
            self.skipTest("This filesystem cannot create hardlinks")
        task = json.loads(self.run_import().read_text(encoding="utf-8"))
        self.assertNotIn("hidden_alias.py", task["files"])
        with self.assertRaisesRegex(ImportTaskError, "editable_test_alias"):
            self.run_import(editable=["hidden_alias.py"], task_id="alias-task")

    def test_windows_reparse_points_are_link_hazards(self):
        from forge.importer import _linked
        class ReparseInfo:
            st_mode = 0o040755
            st_file_attributes = 0x400
        with patch.object(Path, "lstat", return_value=ReparseInfo()):
            self.assertTrue(_linked(self.repo))

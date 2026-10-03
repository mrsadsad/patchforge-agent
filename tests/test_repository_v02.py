import copy
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from forge.artifacts import review_bundle
from forge.repository import Repository, ToolError
from forge.server import make_server


class RepositoryV02Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="patchforge-v02-")
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name)
        self.task = {
            "id": "review-test", "title": "Review behavior", "category": "test", "difficulty": "easy",
            "issue": "Fix the arithmetic.", "split": "development", "sha256": "task-hash",
            "files": {"a.py": "def value():\n    return 1\n", "package/b.py": "NUMBER = 2\n",
                      "test_visible.py": "# immutable test fixture\n", "README.md": "Review this package.\n"},
            "editable": ["a.py", "package/b.py"], "hidden_tests": "# PRIVATE_HIDDEN_SOURCE_SENTINEL\n",
            "demo_edits": [{"path": "a.py", "old": "return 1", "new": "return 3"}],
        }
        self.repo = Repository(self.run_dir / "run-1" / "workspace", self.task)

    def state(self):
        return {"id": "run-1", "task_id": self.task["id"], "status": "completed", "diff": self.repo.diff(),
                "workspace_sha256": self.repo.digest(), "task_sha256": "task-hash", "source_sha256": "source-hash",
                "events": []}

    def test_invalid_second_edit_keeps_entire_batch_unchanged(self):
        before = self.repo.digest()
        with self.assertRaisesRegex(ToolError, "exact_match_count_0"):
            self.repo.apply_edits([{"path": "a.py", "old": "return 1", "new": "return 7"},
                                   {"path": "package/b.py", "old": "absent", "new": "anything"}])
        self.assertEqual(self.repo.digest(), before)
        self.assertEqual(self.repo.diff(), "")

    def test_syntax_error_in_second_file_rejects_all_edits(self):
        before = self.repo.digest()
        with self.assertRaisesRegex(ToolError, "invalid_python_syntax"):
            self.repo.apply_edits([{"path": "a.py", "old": "return 1", "new": "return 7"},
                                   {"path": "package/b.py", "old": "NUMBER = 2", "new": "def broken("}])
        self.assertEqual(self.repo.digest(), before)

    def test_sequential_edits_same_file_validate_final_source(self):
        result = self.repo.apply_edits([{"path": "a.py", "old": "return 1", "new": "return 4"},
                                        {"path": "a.py", "old": "return 4", "new": "return 9"}])
        self.assertTrue(result["ok"])
        self.assertIn("return 9", self.repo.read("a.py"))
        self.assertEqual(result["workspace_sha256"], self.repo.digest())

    def test_transient_second_write_failure_rolls_back_first_file(self):
        before = self.repo.digest()
        original_write = Path.write_text
        failed = False

        def fail_second(path, content, *args, **kwargs):
            nonlocal failed
            if path.name == "b.py" and content == "NUMBER = 8\n" and not failed:
                failed = True
                raise OSError("simulated write failure")
            return original_write(path, content, *args, **kwargs)

        with patch.object(Path, "write_text", fail_second), self.assertRaisesRegex(ToolError, "write_failed_rolled_back"):
            self.repo.apply_edits([{"path": "a.py", "old": "return 1", "new": "return 7"},
                                   {"path": "package/b.py", "old": "NUMBER = 2", "new": "NUMBER = 8"}])
        self.assertTrue(failed)
        self.assertEqual(self.repo.digest(), before)

    def test_readonly_edit_cannot_partially_commit(self):
        before = self.repo.digest()
        with self.assertRaisesRegex(ToolError, "file_is_read_only"):
            self.repo.apply_edits([{"path": "a.py", "old": "return 1", "new": "return 9"},
                                   {"path": "test_visible.py", "old": "immutable", "new": "modified"}])
        self.assertEqual(self.repo.digest(), before)

    def test_line_ranges_are_numbered_limited_and_typed(self):
        content = "".join(f"# line {number}\n" for number in range(1, 351))
        (self.repo.root / "a.py").write_text(content, encoding="utf-8", newline="\n")
        result = self.repo.read_range("a.py", 10, 999)
        self.assertEqual((result["start_line"], result["end_line"], result["total_lines"]), (10, 209, 350))
        self.assertEqual(len(result["content"].splitlines()), 200)
        self.assertTrue(result["truncated"])
        self.assertEqual(self.repo.read_range("a.py")["end_line"], 120)
        for start, end in ((0, 1), (True, 2), (1, False), (5, 2), ("1", 2)):
            with self.subTest(start=start, end=end), self.assertRaisesRegex(ToolError, "invalid_line_range"):
                self.repo.read_range("a.py", start, end)

    def test_range_rejects_oversized_line(self):
        (self.repo.root / "a.py").write_text("#" + "x" * 14_001, encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "range_too_large"):
            self.repo.read_range("a.py", 1, 1)

    def test_checkpoint_restore_recovers_multiple_editable_files(self):
        before = self.repo.digest()
        saved = self.repo.save_checkpoint("before changes")
        self.repo.apply_edits([{"path": "a.py", "old": "return 1", "new": "return 9"},
                               {"path": "package/b.py", "old": "NUMBER = 2", "new": "NUMBER = 8"}])
        self.assertNotEqual(self.repo.digest(), before)
        result = self.repo.restore_checkpoint(saved["id"])
        self.assertEqual(result["workspace_sha256"], before)
        self.assertEqual(self.repo.changed(), 0)
        self.assertEqual(self.repo.read("test_visible.py"), self.task["files"]["test_visible.py"])
        with self.assertRaisesRegex(ToolError, "invalid_checkpoint_id"):
            self.repo.restore_checkpoint("../checkpoint")

    def test_checkpoint_with_unexpected_files_cannot_modify_workspace(self):
        saved = self.repo.save_checkpoint("before")
        checkpoint = self.repo.root.parent / "checkpoints" / (saved["id"] + ".json")
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        payload["files"]["test_visible.py"] = "tampered\n"
        checkpoint.write_text(json.dumps(payload), encoding="utf-8")
        before = self.repo.digest()
        with self.assertRaisesRegex(ToolError, "checkpoint_files_mismatch"):
            self.repo.restore_checkpoint(saved["id"])
        self.assertEqual(self.repo.digest(), before)

    def test_bundle_is_deterministic_allowlisted_and_hash_verifiable(self):
        self.repo.save_checkpoint("private checkpoint label")
        (self.repo.root / ".env").write_text("PRIVATE_ENV_SENTINEL", encoding="utf-8")
        (self.repo.root / "test_hidden.py").write_text(self.task["hidden_tests"], encoding="utf-8")
        self.repo.replace("a.py", "return 1", "return 5")
        state = self.state()
        first = review_bundle(state, self.repo)
        self.assertEqual(first, review_bundle(state, self.repo))
        expected = {"manifest.json", "result.json", "task.json", "patch.diff"}
        expected |= {f"{version}/{name}" for version in ("before", "after") for name in self.task["files"]}
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertEqual(set(archive.namelist()), expected)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(set(manifest["files"]), expected - {"manifest.json"})
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)
            self.assertEqual(manifest["workspace_sha256"], self.repo.digest())
            all_bytes = b"\n".join(archive.read(name) for name in archive.namelist())
            for sentinel in (b"PRIVATE_ENV_SENTINEL", b"PRIVATE_HIDDEN_SOURCE_SENTINEL", b"private checkpoint label", b"demo_edits"):
                self.assertNotIn(sentinel, all_bytes)
            self.assertNotEqual(archive.read("before/a.py"), archive.read("after/a.py"))

    def test_file_apis_allowlist_paths_and_bundle_checks_completion_digest(self):
        state = self.state()
        config = SimpleNamespace(run_dir=self.run_dir, image="python:3.12-slim", model="test", api_key="")
        engine = SimpleNamespace(config=config, tasks={self.task["id"]: self.task},
                                 get=lambda identifier: copy.deepcopy(state), list=lambda: [copy.deepcopy(state)],
                                 repository=lambda identifier: self.repo)
        with patch("forge.server.docker_status", return_value={"docker_available": True, "image_available": True}):
            server = make_server(engine, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(path):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        status, raw = request("/api/tasks/review-test/files")
        self.assertEqual(status, 200)
        self.assertEqual({file["path"] for file in json.loads(raw)["files"]}, set(self.task["files"]))
        for prefix in ("/api/tasks/review-test", "/api/runs/run-1"):
            status, raw = request(prefix + "/file?path=a.py")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["content"], self.task["files"]["a.py"])
            for path in ("test_hidden.py", ".env", "../checkpoints/anything.json"):
                self.assertEqual(request(prefix + "/file?path=" + path)[0], 404)
        self.assertEqual(request("/api/runs/run-1/bundle")[0], 200)
        state["status"] = "running"
        self.assertEqual(request("/api/runs/run-1/bundle")[0], 409)
        state["status"] = "completed"
        self.repo.replace("a.py", "return 1", "return 10")
        status, raw = request("/api/runs/run-1/bundle")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw)["error"], "workspace_changed_since_completion")

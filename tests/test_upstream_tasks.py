"""Validate provenance without execution; run public-history replays only in Docker."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from forge.repository import Repository
from forge.sandbox import Sandbox, docker_status


ROOT = Path(__file__).resolve().parents[1]


def tasks():
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((ROOT / "tasks").glob("upstream-*.json"))]


class UpstreamTaskIntegrityTests(unittest.TestCase):
    def test_pinned_provenance_license_and_original_source_hashes(self):
        fixtures = tasks()
        self.assertEqual(len(fixtures), 2)
        for task in fixtures:
            with self.subTest(task=task["id"]):
                self.assertEqual(task["split"], "upstream-replay")
                provenance = task["provenance"]
                self.assertEqual(provenance["kind"], "upstream_historical")
                self.assertEqual(provenance["license"], "MIT")
                self.assertIn("Thomas Kemmer", task["files"]["LICENSE"])
                self.assertIn("Permission is hereby granted", task["files"]["LICENSE"])
                for field in ("base_commit", "fix_commit"):
                    self.assertRegex(provenance[field], r"^[a-f0-9]{40}$")
                self.assertEqual(len(provenance["files"]), 6)
                for name, source in provenance["files"].items():
                    self.assertEqual(hashlib.sha256(task["files"][name].encode()).hexdigest(), source["sha256"])
                    self.assertIn(provenance["base_commit"], source["source_url"])
                self.assertNotIn("class WithheldBoundaries", "\n".join(task["files"].values()))

    def test_reference_edit_reconstructs_upstream_fixed_source(self):
        for task in tasks():
            with self.subTest(task=task["id"]):
                source = task["files"]["cachetools/__init__.py"]
                self.assertEqual(task["editable"], ["cachetools/__init__.py"])
                for edit in task["demo_edits"]:
                    self.assertEqual(edit["path"], "cachetools/__init__.py")
                    self.assertEqual(source.count(edit["old"]), 1)
                    source = source.replace(edit["old"], edit["new"], 1)
                self.assertEqual(hashlib.sha256(source.encode()).hexdigest(), task["provenance"]["fixed_source_sha256"])


@unittest.skipUnless(os.environ.get("PATCHFORGE_DOCKER_TESTS") == "1", "Set PATCHFORGE_DOCKER_TESTS=1 for isolated replay tests")
class UpstreamReplayDockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = docker_status("python:3.12-slim")
        if not state["docker_available"] or not state["image_available"]:
            raise unittest.SkipTest("Docker and the existing python:3.12-slim image are required")

    def test_baselines_fail_and_exact_upstream_fixes_pass(self):
        sandbox = Sandbox(timeout=25)
        for task in tasks():
            with self.subTest(task=task["id"]), tempfile.TemporaryDirectory(prefix="patchforge-upstream-") as temporary:
                repo = Repository(Path(temporary) / "workspace", task)
                baseline = sandbox.run(repo.root, {"test_visible.py": task["files"]["test_visible.py"]})
                self.assertFalse(baseline["passed"], baseline)
                self.assertGreater(baseline["failures"], 0, baseline)
                self.assertEqual(baseline["errors"], 0, baseline)
                for edit in task["demo_edits"]:
                    repo.replace(**edit)
                result = sandbox.run(repo.root, {"test_visible.py": task["files"]["test_visible.py"], "test_hidden.py": task["hidden_tests"]})
                self.assertTrue(result["passed"], result)
                self.assertEqual(result["total"], 7)

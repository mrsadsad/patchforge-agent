"""Runtime boundary tests; fake execution is never presented as Docker evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from forge.config import Config
from forge.provider import ProviderError
from forge.runtime import Engine


BROKEN = "value = 'broken'\n"
FIXED = "value = 'fixed'\n"
REGRESSION = "value = 'regression'\n"
SECRET = "WITHHELD_SENTINEL_NOT_FOR_MODEL"


def call(name, arguments):
    return {"id": "call-1", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


def edit(old=BROKEN, new=FIXED, path="solution.py"):
    return {"path": path, "old": old, "new": new}


class FakeSandbox:
    def __init__(self, hidden_passes=True):
        self.calls = []
        self.hidden_passes = hidden_passes
        self.before_return = None

    def run(self, root, tests, cancel=None):
        source = (root / "solution.py").read_text(encoding="utf-8")
        hidden = "test_hidden.py" in tests
        self.calls.append({"source": source, "hidden": hidden, "tests": dict(tests)})
        if self.before_return:
            self.before_return()
        passed = source == FIXED and (not hidden or self.hidden_passes)
        return {"passed": passed, "total": 1, "failures": int(not passed),
                "errors": 0, "skipped": 0,
                "output": SECRET if hidden else "synthetic fake executor result", "elapsed_ms": 1}


class FakeProvider:
    def __init__(self, actions, tokens=7):
        self.actions = list(actions)
        self.requests = []
        self.tokens = tokens
        self.before_return = None

    def complete(self, messages, tools):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        if not self.actions:
            raise AssertionError("unexpected model request")
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        if self.before_return:
            self.before_return()
        return {"message": {"role": "assistant", "content": None, "tool_calls": [action]},
                "tokens": self.tokens}


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="patchforge-runtime-test-")
        self.root = Path(self.temp.name)
        self.task_dir = self.root / "tasks"
        self.task_dir.mkdir()
        task = {"id": "fixture", "title": "Synthetic fixture", "category": "test",
                "difficulty": "easy", "split": "development", "issue": "Repair the value.",
                "files": {"solution.py": BROKEN, "test_visible.py": "# immutable visible test"},
                "editable": ["solution.py"], "hidden_tests": SECRET,
                "demo_edits": [edit()]}
        (self.task_dir / "fixture.json").write_text(json.dumps(task), encoding="utf-8")
        self.config = Config(task_dir=self.task_dir, run_dir=self.root / "runs")
        self.engines = []

    def tearDown(self):
        for engine in self.engines:
            engine.close()
        self.temp.cleanup()

    def engine(self, sandbox=None, provider=None):
        engine = Engine(self.config, sandbox=sandbox or FakeSandbox(), provider=provider)
        self.engines.append(engine)
        return engine

    def assert_model_has_no_answers(self, provider):
        serialized = json.dumps(provider.requests)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("demo_edits", serialized)

    def test_demo_verifies_reference_patch_without_model_calls(self):
        sandbox, provider = FakeSandbox(), FakeProvider([])
        state = self.engine(sandbox, provider).create("fixture", background=False)
        self.assertEqual(state["status"], "completed")
        self.assertFalse(state["baseline"]["passed"])
        self.assertTrue(state["visible"]["passed"])
        self.assertTrue(state["hidden"]["passed"])
        self.assertEqual(state["tokens"], 0)
        self.assertEqual(provider.requests, [])
        self.assertEqual([run["hidden"] for run in sandbox.calls], [False, False, True])
        self.assertTrue((self.config.run_dir / state["id"] / "patch.diff").is_file())

    def test_direct_patch_gets_both_final_checks_and_no_answer_leakage(self):
        provider = FakeProvider([call("submit_patch", {"edits": [edit()], "summary": "Fixed value"})])
        sandbox = FakeSandbox()
        state = self.engine(sandbox, provider).create("fixture", mode="live", strategy="direct", background=False)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(state["steps"], 1)
        self.assertEqual(state["tokens"], 7)
        self.assertEqual([run["source"] for run in sandbox.calls], [BROKEN, FIXED, FIXED])
        self.assert_model_has_no_answers(provider)

    def test_finish_retests_bytes_changed_after_earlier_success(self):
        provider = FakeProvider([
            call("replace_text", edit()), call("run_tests", {}),
            call("replace_text", edit(FIXED, REGRESSION)), call("finish", {"summary": "Claimed fix"})])
        sandbox = FakeSandbox()
        state = self.engine(sandbox, provider).create("fixture", mode="live", max_steps=4, background=False)
        self.assertNotEqual(state["status"], "completed")
        self.assertFalse(state["visible"]["passed"])
        self.assertIsNone(state["hidden"])
        self.assertEqual([run["source"] for run in sandbox.calls], [BROKEN, FIXED, REGRESSION])
        errors = [event["data"].get("error") for event in state["events"]]
        self.assertIn("visible_tests_failed", errors)

    def test_hidden_failure_is_terminal_without_feedback_to_model(self):
        provider = FakeProvider([call("replace_text", edit()), call("finish", {"summary": "Fix"})])
        state = self.engine(FakeSandbox(hidden_passes=False), provider).create(
            "fixture", mode="live", max_steps=8, background=False)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "hidden_tests_failed")
        self.assertEqual(len(provider.requests), 2)
        self.assertTrue(state["visible"]["passed"])
        self.assertFalse(state["hidden"]["passed"])
        self.assert_model_has_no_answers(provider)

    def test_visible_tests_cannot_be_edited_by_model(self):
        provider = FakeProvider([call("replace_text", edit(
            old="# immutable visible test", new="# disabled", path="test_visible.py"))])
        state = self.engine(provider=provider).create("fixture", mode="live", max_steps=1, background=False)
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(state["diff"], "")
        test_path = self.config.run_dir / state["id"] / "workspace" / "test_visible.py"
        self.assertEqual(test_path.read_text(), "# immutable visible test")
        errors = [event["data"].get("error") for event in state["events"]]
        self.assertIn("file_is_read_only", errors)

    def test_cancellation_during_baseline_prevents_patch_and_model_request(self):
        sandbox, provider = FakeSandbox(), FakeProvider([])
        engine = self.engine(sandbox, provider)
        sandbox.before_return = lambda: engine.cancel(next(iter(engine.states)))
        state = engine.create("fixture", mode="live", background=False)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["diff"], "")
        self.assertEqual(provider.requests, [])
        self.assertEqual(len(sandbox.calls), 1)

    def test_cancelled_model_response_is_counted_but_never_applied(self):
        provider = FakeProvider([call("replace_text", edit())])
        sandbox = FakeSandbox()
        engine = self.engine(sandbox, provider)
        provider.before_return = lambda: engine.cancel(next(iter(engine.states)))
        state = engine.create("fixture", mode="live", background=False)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["tokens"], 7)
        self.assertEqual(state["diff"], "")
        self.assertEqual(state["tool_calls"], 0)
        self.assertEqual(len(sandbox.calls), 1)

    def test_transport_failure_does_not_claim_complete_usage(self):
        provider = FakeProvider([ProviderError("provider_connection_failed")])
        state = self.engine(provider=provider).create("fixture", mode="live", background=False)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "provider_connection_failed")
        self.assertFalse(state["usage_complete"])
        self.assertIsNone(state["tokens"])

    def test_missing_usage_never_becomes_a_verified_zero(self):
        provider = FakeProvider([call("submit_patch", {"edits": [edit()], "summary": "Fix"})], tokens=None)
        state = self.engine(provider=provider).create("fixture", mode="live", strategy="direct", background=False)
        self.assertEqual(state["status"], "completed")
        self.assertIsNone(state["tokens"])
        self.assertFalse(state["usage_complete"])

    def test_restart_marks_inflight_work_interrupted(self):
        first = self.engine()
        state = first.create("fixture", background=False)
        state.update(status="running", phase="model")
        first.store.save(state)
        first.close()
        second = self.engine()
        recovered = second.get(state["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["error"], "service_restarted")
        self.assertFalse(recovered["usage_complete"])


if __name__ == "__main__":
    unittest.main()

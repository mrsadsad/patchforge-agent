"""Context budgets, answer isolation, and complete tool-turn preservation."""
import copy
import json
import unittest

from forge.context import build_task_context, compact_messages


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


def task(files=None):
    return {"issue": "Repair expired cache entries at the TTL boundary.",
            "editable": ["cache.py"],
            "files": files or {"cache.py": "def expired(now, deadline):\n    return now > deadline\n",
                               "test_cache.py": "assert expired(10, 10)\n"},
            "hidden_tests": "WITHHELD_SECRET", "demo_edits": [{"new": "REFERENCE_SECRET"}],
            "provenance": {"fix_commit": "FIX_COMMIT_SECRET"}}


def group(index, amount=6000, calls=1):
    operations, results = [], []
    for offset in range(calls):
        identifier = f"call-{index}-{offset}"
        operations.append({"id": identifier, "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": f"module_{offset}.py"})}})
        results.append({"role": "tool", "tool_call_id": identifier, "content": json.dumps({
            "ok": True, "content": "SOURCE_CONTENT_SECRET" + "x" * amount})})
    return [{"role": "assistant", "tool_calls": operations,
             "content": "ASSISTANT_PROSE_SECRET", "reasoning_content": "PRIVATE_REASONING_SECRET"}] + results


PREFIX = [{"role": "system", "content": "System policy"}, {"role": "user", "content": "Initial task"}]


class InitialContextTests(unittest.TestCase):
    def test_small_task_keeps_all_original_visible_sources(self):
        original = task()
        saved = copy.deepcopy(original)
        selected = build_task_context(original, {"output": "failure"})
        self.assertEqual(selected["files"], original["files"])
        self.assertEqual(selected["omitted_files"], [])
        self.assertEqual(original, saved)
        self.assertEqual(set(selected), {"issue", "editable", "files", "file_index", "omitted_files", "context_note"})
        encoded = json.dumps(selected)
        for secret in ("WITHHELD_SECRET", "REFERENCE_SECRET", "FIX_COMMIT_SECRET"):
            self.assertNotIn(secret, encoded)

    def test_ast_index_includes_qualified_methods_and_async_functions(self):
        sample = task({"cache.py": "class Cache:\n    async def expired(self):\n        return False\n"})
        selected = build_task_context(sample, {})
        symbols = selected["file_index"][0]["symbols"]
        self.assertEqual(symbols, [{"name": "Cache", "line": 1, "kind": "class"},
                                   {"name": "Cache.expired", "line": 2, "kind": "function"}])
        self.assertEqual(selected["file_index"][0]["lines"], 3)
        self.assertEqual(selected["file_index"][0]["bytes"], len(sample["files"]["cache.py"].encode()))

    def test_broken_python_remains_available_without_symbol_parse_crash(self):
        selected = build_task_context(task({"cache.py": "def broken(:\n"}), {})
        self.assertEqual(selected["file_index"][0]["symbols"], [])
        self.assertEqual(selected["files"]["cache.py"], "def broken(:\n")

    def test_bounded_selection_retains_implementation_tests_and_relevant_late_lines(self):
        files = {"cache.py": "# unrelated padding\n" * 900 + "def ttl_boundary(now, deadline):\n    return now > deadline\n",
                 "test_cache.py": "def test_boundary():\n    assert expired(10, 10)\n"}
        files.update({f"unrelated_{i}.txt": "unrelated document " * 2000 for i in range(8)})
        selected = build_task_context(task(files), {"output": "failure in ttl_boundary"}, max_chars=4500)
        self.assertLessEqual(size(selected), 4500)
        self.assertIn("cache.py", selected["files"])
        self.assertIn("test_cache.py", selected["files"])
        self.assertIn("ttl_boundary", selected["files"]["cache.py"])
        self.assertIn("Excerpt:", selected["files"]["cache.py"])
        self.assertIn("read_file", selected["context_note"])

    def test_actual_serialized_budget_accounts_for_unicode_and_escaping(self):
        files = {"cache.py": ('value = "\\\\引号"\n' * 3000),
                 "test_cache.py": "# 可见测试\n" * 400}
        selected = build_task_context(task(files), {}, max_chars=1800)
        self.assertLessEqual(size(selected), 1800)
        self.assertTrue(selected["files"])

    def test_direct_and_repair_have_identical_initial_evidence(self):
        sample = task({"cache.py": "# padding\n" * 3000, "test_cache.py": "assert True\n"})
        baseline = {"output": "TTL failure"}
        self.assertEqual(build_task_context(sample, baseline, direct=False, max_chars=2000),
                         build_task_context(sample, baseline, direct=True, max_chars=2000))

    def test_metadata_too_large_is_an_explicit_error(self):
        sample = task()
        sample["issue"] = "Required issue evidence " * 1000
        with self.assertRaisesRegex(ValueError, "context_metadata_exceeds_budget"):
            build_task_context(sample, {}, max_chars=1000)


class HistoryCompactionTests(unittest.TestCase):
    def test_under_budget_history_is_unchanged_and_detached(self):
        messages = copy.deepcopy(PREFIX) + group(0, amount=10)
        result, stats = compact_messages(messages)
        self.assertEqual(result, messages)
        self.assertEqual(stats["compacted_groups"], 0)
        self.assertFalse(stats["over_limit"])
        result[0]["content"] = "changed"
        self.assertEqual(messages[0]["content"], "System policy")

    def test_only_old_complete_groups_are_replaced_and_recent_group_is_exact(self):
        messages = copy.deepcopy(PREFIX) + group(0, calls=2) + group(1, amount=100)
        saved = copy.deepcopy(messages)
        result, stats = compact_messages(messages, max_chars=2500, keep_recent_groups=1)
        self.assertEqual(result[:2], PREFIX)
        self.assertEqual(result[-2:], group(1, amount=100))
        self.assertEqual(messages, saved)
        self.assertEqual(stats["compacted_groups"], 1)
        self.assertFalse(stats["over_limit"])
        self.assertEqual(stats["before_chars"], size(messages))
        self.assertEqual(stats["after_chars"], size(result))
        summary = result[2]["content"]
        self.assertIn("read_file", summary)
        self.assertIn("module_0.py", summary)
        self.assertIn('"ok": true', summary)
        for secret in ("PRIVATE_REASONING_SECRET", "ASSISTANT_PROSE_SECRET", "SOURCE_CONTENT_SECRET"):
            self.assertNotIn(secret, summary)
        self.assertLessEqual(len(summary), 1600)

    def test_multicall_summary_is_capped_without_orphan_results(self):
        messages = copy.deepcopy(PREFIX) + group(0, calls=16)
        result, stats = compact_messages(messages, max_chars=2000, keep_recent_groups=0)
        self.assertFalse(stats["over_limit"])
        self.assertEqual(len(result), 3)
        self.assertEqual(result[2]["role"], "user")
        self.assertLessEqual(len(result[2]["content"]), 1600)
        self.assertIn('"total_operations": 16', result[2]["content"])

    def test_protected_recent_groups_can_report_over_limit_without_cutting(self):
        messages = copy.deepcopy(PREFIX) + group(0)
        result, stats = compact_messages(messages, max_chars=1000, keep_recent_groups=1)
        self.assertEqual(result, messages)
        self.assertTrue(stats["over_limit"])
        self.assertEqual(stats["compacted_groups"], 0)

    def test_oversized_initial_task_is_never_truncated(self):
        messages = copy.deepcopy(PREFIX)
        messages[1]["content"] = "issue" * 1000
        messages += group(0)
        result, stats = compact_messages(messages, max_chars=1000, keep_recent_groups=0)
        self.assertEqual(result[:2], messages[:2])
        self.assertTrue(stats["over_limit"])

    def test_malformed_and_incomplete_tool_groups_are_refused(self):
        cases = [group(0)[:1], group(0)[1:], group(0, calls=2)[:-1],
                 [{"role": "assistant", "tool_calls": {}}]]
        duplicate = group(0, calls=2)
        duplicate[2]["tool_call_id"] = duplicate[1]["tool_call_id"]
        cases.append(duplicate)
        malformed = group(0)
        malformed[1]["tool_call_id"] = ["unhashable"]
        cases.append(malformed)
        for history in cases:
            with self.subTest(history=history), self.assertRaisesRegex(ValueError, "malformed_tool_group"):
                compact_messages(copy.deepcopy(PREFIX) + history)

    def test_non_tool_turns_are_preserved_without_fabricated_summaries(self):
        reminder = [{"role": "assistant", "content": "No tool used"},
                    {"role": "user", "content": "Please act using a tool"}]
        result, stats = compact_messages(copy.deepcopy(PREFIX) + reminder + group(0),
                                          max_chars=1000, keep_recent_groups=0)
        self.assertEqual(result[:4], PREFIX + reminder)
        self.assertFalse(stats["over_limit"])

    def test_test_status_is_recorded_without_claiming_current_correctness(self):
        messages = copy.deepcopy(PREFIX) + group(0)
        messages[2]["tool_calls"][0]["function"] = {"name": "run_tests", "arguments": "{}"}
        messages[3]["content"] = json.dumps({"passed": False, "total": 5, "failures": 2,
                                               "output": "PRIVATE_OUTPUT" * 1000})
        result, stats = compact_messages(messages, max_chars=1000, keep_recent_groups=0)
        self.assertFalse(stats["over_limit"])
        self.assertIn('"passed": false', result[-1]["content"])
        self.assertIn('"failures": 2', result[-1]["content"])
        self.assertIn("may be stale", result[-1]["content"])
        self.assertNotIn("PRIVATE_OUTPUT", result[-1]["content"])

    def test_invalid_argument_json_is_a_complete_recoverable_tool_turn(self):
        messages = copy.deepcopy(PREFIX) + group(0)
        messages[2]["tool_calls"][0]["function"]["arguments"] = '{"path": "unfinished'
        messages[3]["content"] = json.dumps({"ok": False, "error": "invalid_arguments",
                                              "hint": "repair JSON" * 1000})
        result, stats = compact_messages(messages, max_chars=1000, keep_recent_groups=0)
        self.assertFalse(stats["over_limit"])
        self.assertIn("invalid_arguments", result[-1]["content"])
        self.assertNotIn("unfinished", result[-1]["content"])

    def test_repeated_compaction_keeps_summaries_and_new_tool_groups_valid(self):
        messages = copy.deepcopy(PREFIX) + group(0) + group(1) + group(2, amount=10)
        compacted, first = compact_messages(messages, max_chars=2000, keep_recent_groups=1)
        self.assertFalse(first["over_limit"])
        summaries = [message for message in compacted[2:] if message["role"] == "user"]
        compacted += group(3, amount=100)
        result, second = compact_messages(compacted, max_chars=2000, keep_recent_groups=1)
        self.assertFalse(second["over_limit"])
        self.assertEqual(result[:2], PREFIX)
        self.assertEqual(result[-2:], group(3, amount=100))
        self.assertTrue(all(summary in result for summary in summaries))
        # Validation on the compacted output must find no orphan or incomplete tools.
        same, stats = compact_messages(result, max_chars=10000)
        self.assertEqual(same, result)
        self.assertFalse(stats["over_limit"])


if __name__ == "__main__":
    unittest.main()

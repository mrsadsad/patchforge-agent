"""Rebuild pinned public-history tasks; downloads data but never runs upstream code."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
from pathlib import Path
import tarfile
from textwrap import dedent
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/tkem/cachetools"
CASES = [
    {
        "id": "upstream-cachetools-expired-overwrite",
        "title": "真实缺陷 · TLRU 过期覆盖保留旧值",
        "base": "978d34d40cdbf3b4cbfce104f3166032cc4ea028",
        "fix": "c0fdf6abab38040947d6fe2e38c507401d5e2350",
        "issue": """Historical public development replay of an upstream cachetools defect. Repair the actual package source while preserving its public APIs. This is not a blind benchmark.

TLRUCache uses ttu(key, value, now) to decide an item's deadline. Assigning a replacement whose deadline is equal to or before now currently leaves the previous value available. An assignment of an already-expired replacement must instead remove any prior entry for that key. Assigning an expired value to an absent key remains a no-op. Keep membership, reads, iteration, currsize, expiration bookkeeping and future reinsertion consistent. Other keys and ordinary future-dated replacements must retain their existing behavior. Timers are injected; no real sleeps or network are necessary.
""",
        "visible": '''
import unittest
from cachetools import TLRUCache

class VisibleRegression(unittest.TestCase):
    def test_dead_on_arrival_replacement_removes_previous_value(self):
        cache = TLRUCache(3, ttu=lambda key, value, now: now + value, timer=lambda: 20)
        cache["account"] = 9
        cache["account"] = 0
        self.assertNotIn("account", cache)
        self.assertEqual(cache.get("account", "missing"), "missing")
        self.assertEqual((list(cache), cache.currsize), ([], 0))

    def test_expired_new_key_remains_absent(self):
        cache = TLRUCache(3, ttu=lambda key, value, now: now + value, timer=lambda: 20)
        cache["new"] = 0
        self.assertEqual(len(cache), 0)

    def test_future_replacement_is_stored(self):
        cache = TLRUCache(3, ttu=lambda key, value, now: now + value, timer=lambda: 20)
        cache["account"] = 3
        cache["account"] = 12
        self.assertEqual(cache["account"], 12)
        self.assertEqual(cache.currsize, 1)
''',
        "hidden": '''
import unittest
from cachetools import TLRUCache

class WithheldBoundaries(unittest.TestCase):
    def test_past_deadline_and_other_keys(self):
        cache = TLRUCache(3, ttu=lambda key, value, now: now + value, timer=lambda: 10)
        cache["victim"] = 8
        cache["keep"] = 11
        cache["victim"] = -5
        with self.assertRaises(KeyError):
            cache["victim"]
        self.assertEqual(dict(cache), {"keep": 11})

    def test_custom_sizes_release_capacity(self):
        cache = TLRUCache(10, ttu=lambda key, value, now: now + value[0],
                          timer=lambda: 10, getsizeof=lambda value: value[1])
        cache["a"] = (20, 6)
        cache["b"] = (20, 4)
        cache["a"] = (0, 100)
        self.assertEqual(cache.currsize, 4)
        cache["c"] = (20, 6)
        self.assertEqual(set(cache), {"b", "c"})
        self.assertEqual(cache.currsize, 10)

    def test_reinserted_key_survives_old_heap_record(self):
        now = [0]
        cache = TLRUCache(3, ttu=lambda key, value, time: time + value, timer=lambda: now[0])
        cache["key"] = 4
        cache["key"] = 0
        cache["key"] = 12
        now[0] = 5
        self.assertEqual(list(cache.expire()), [])
        self.assertEqual(cache["key"], 12)
        now[0] = 12
        self.assertEqual(list(cache.expire()), [("key", 12)])
        self.assertEqual(cache.currsize, 0)

    def test_already_stale_entry_and_repeated_expired_assignment(self):
        now = [0]
        cache = TLRUCache(2, ttu=lambda key, value, time: time + value, timer=lambda: now[0])
        cache["key"] = 2
        now[0] = 5
        cache["key"] = 0
        cache["key"] = -1
        self.assertEqual(list(cache.expire()), [])
        self.assertEqual(cache.currsize, 0)
        cache["key"] = 8
        self.assertEqual(cache["key"], 8)
''',
    },
    {
        "id": "upstream-cachetools-growing-entry",
        "title": "真实缺陷 · 缓存扩容覆盖过度驱逐",
        "base": "16e88894ef7d79b25a68f5a3b5411ed881342725",
        "fix": "39b31bc9b63abe98497409945e9d382d8918c8fb",
        "upstream_issue": REPOSITORY + "/issues/405",
        "issue": """Historical public development replay of an upstream cachetools defect. Repair the actual package source while preserving its public APIs. This is not a blind benchmark.

When getsizeof is configured and an existing value grows, Cache.__setitem__ can evict unrelated entries even when the replacement fits. Its current size already includes the old value. Capacity accounting must reflect the replacement rather than treating it as an additional complete entry. Preserve the eviction policy of each derived cache. If eviction removes the very key being replaced, subsequent capacity accounting must still be correct for inserting the replacement. Keep currsize equal to the actual summed item sizes. Equal-sized replacements, shrinking values, new insertions and oversized-value errors must retain their behavior.
""",
        "visible": '''
import unittest
from cachetools import FIFOCache

class VisibleRegression(unittest.TestCase):
    def test_growth_that_fits_preserves_other_entries(self):
        cache = FIFOCache(10, getsizeof=len)
        cache["a"] = "aaa"
        cache["b"] = "bbbb"
        cache["b"] = "BBBBBBB"
        self.assertEqual(dict(cache), {"a": "aaa", "b": "BBBBBBB"})
        self.assertEqual(cache.currsize, 10)

    def test_equal_size_replacement(self):
        cache = FIFOCache(8, getsizeof=len)
        cache["a"] = "abc"
        cache["b"] = "defg"
        cache["b"] = "DEFG"
        self.assertEqual((len(cache), cache.currsize), (2, 7))

    def test_oversized_replacement_preserves_original(self):
        cache = FIFOCache(5, getsizeof=len)
        cache["a"] = "abc"
        with self.assertRaises(ValueError):
            cache["a"] = "too-large"
        self.assertEqual((cache["a"], cache.currsize), ("abc", 3))
''',
        "hidden": '''
import unittest
from cachetools import Cache, FIFOCache, LRUCache, LFUCache, RRCache, TTLCache, TLRUCache

class WithheldBoundaries(unittest.TestCase):
    def test_growth_policy_shared_by_all_cache_types(self):
        factories = [Cache, FIFOCache, LRUCache, LFUCache, RRCache,
                     lambda **kw: TTLCache(ttl=100, timer=lambda: 0, **kw),
                     lambda **kw: TLRUCache(ttu=lambda k, v, t: t + 100, timer=lambda: 0, **kw)]
        for factory in factories:
            with self.subTest(factory=factory):
                cache = factory(maxsize=12, getsizeof=len)
                cache["keep"] = "abcde"
                cache["grow"] = "xy"
                cache["grow"] = "1234567"
                self.assertEqual(set(cache), {"keep", "grow"})
                self.assertEqual(cache.currsize, 12)

    def test_evicted_replacement_key_requires_full_reservation(self):
        cache = FIFOCache(10, getsizeof=len)
        cache["a"] = "aaa"
        cache["b"] = "bbb"
        cache["c"] = "ccc"
        cache["a"] = "AAAAA"
        self.assertEqual(dict(cache), {"c": "ccc", "a": "AAAAA"})
        self.assertEqual(cache.currsize, 8)

    def test_lru_evicts_only_necessary_unrelated_entry(self):
        cache = LRUCache(10, getsizeof=len)
        cache["a"] = "aaa"
        cache["b"] = "bbb"
        cache["c"] = "ccc"
        _ = cache["a"]
        cache["a"] = "AAAAA"
        self.assertEqual(set(cache), {"a", "c"})
        self.assertEqual(cache.currsize, 8)

    def test_shrinking_zero_size_and_new_insertion(self):
        cache = FIFOCache(10, getsizeof=len)
        cache["a"] = "aaa"
        cache["b"] = "bbbb"
        cache["b"] = ""
        self.assertEqual(cache.currsize, 3)
        cache["c"] = "1234567"
        self.assertEqual(set(cache), {"a", "b", "c"})
        self.assertEqual(cache.currsize, sum(map(len, cache.values())))
''',
    },
]


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "PatchForge-Upstream-Replay"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = response.read(5_000_001)
    if len(result) > 5_000_000:
        raise ValueError("upstream_response_too_large")
    return result


def upstream_snapshot(commit):
    raw = fetch(f"https://codeload.github.com/tkem/cachetools/tar.gz/{commit}")
    files, manifests = {}, {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive.getmembers():
            path = "/".join(member.name.split("/")[1:])
            if not member.isfile() or not (path == "LICENSE" or (path.startswith("src/cachetools/") and path.endswith(".py"))):
                continue
            if member.size > 40_000:
                raise ValueError("upstream_file_too_large")
            content = archive.extractfile(member).read()
            local = path.removeprefix("src/")
            files[local] = content.decode("utf-8")
            manifests[local] = {"upstream_path": path, "sha256": hashlib.sha256(content).hexdigest(),
                                "source_url": f"{REPOSITORY}/blob/{commit}/{path}"}
    if len(files) != 6 or "LICENSE" not in files:
        raise ValueError("unexpected_upstream_package_layout")
    return files, manifests


def reference_edits(before, after):
    edits = []
    old_lines, new_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for group in matcher.get_grouped_opcodes(3):
        old = "".join(old_lines[group[0][1]:group[-1][2]])
        new = "".join(new_lines[group[0][3]:group[-1][4]])
        if before.count(old) != 1:
            raise ValueError("ambiguous_upstream_patch")
        edits.append({"path": "cachetools/__init__.py", "old": old, "new": new})
    applied = before
    for edit in edits:
        applied = applied.replace(edit["old"], edit["new"], 1)
    if applied != after or not edits:
        raise ValueError("reference_patch_mismatch")
    return edits


def build(case):
    files, manifest = upstream_snapshot(case["base"])
    fixed = fetch(f"https://raw.githubusercontent.com/tkem/cachetools/{case['fix']}/src/cachetools/__init__.py")
    edits = reference_edits(files["cachetools/__init__.py"], fixed.decode("utf-8"))
    files["test_visible.py"] = dedent(case["visible"]).lstrip("\n")
    return {
        "id": case["id"], "title": case["title"], "category": "真实开源缺陷回放",
        "difficulty": "medium", "split": "upstream-replay", "issue": case["issue"],
        "files": files, "editable": ["cachetools/__init__.py"],
        "hidden_tests": dedent(case["hidden"]).lstrip("\n"), "demo_edits": edits,
        "demo_available": True,
        "provenance": {
            "kind": "upstream_historical", "repository_url": REPOSITORY,
            "base_commit": case["base"], "fix_commit": case["fix"],
            "source_url": f"{REPOSITORY}/commit/{case['fix']}", "license": "MIT",
            "issue_url": case.get("upstream_issue"), "files": manifest,
            "fixed_source_sha256": hashlib.sha256(fixed).hexdigest(),
            "reproduction_note": "Public historical development replay; source bytes and MIT license are unchanged from the pinned parent. Only src/ is removed from package paths. Regression and boundary tests are authored for PatchForge. Reference edits reconstruct the upstream fixed source exactly. This is not a blind holdout or SWE-bench; public fixes may occur in model training data.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "tasks")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        task = build(case)
        output = args.output / f"{task['id']}.json"
        output.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"{output.name}: {len(task['files'])} files, {len(task['demo_edits'])} upstream edit(s)")


if __name__ == "__main__":
    main()

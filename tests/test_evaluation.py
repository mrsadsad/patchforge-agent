import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge import evaluation


class FakeEngine:
    def __init__(self, root):
        self.config = SimpleNamespace(task_dir=root / 'tasks', run_dir=root / 'runs',
                                      image='local:test', model='fake-model', api_key='')
        self.config.task_dir.mkdir()
        self.tasks, self.calls = {}, []
        self.provider = object()
        self.raise_on_create = False
        self.bad_run_hash = False
        for name, split in [('development-case', 'development'), ('upstream-case', 'upstream-replay')]:
            data = {'id': name, 'split': split, 'files': {'source.py': 'pass\n'},
                    'editable': ['source.py'], 'issue': 'Example', 'demo_edits': [{'path': 'source.py'}]}
            raw = json.dumps(data).encode()
            (self.config.task_dir / (name + '.json')).write_bytes(raw)
            self.tasks[name] = data | {'sha256': hashlib.sha256(raw).hexdigest()}

    def source_hash(self):
        return evaluation._source_hash(Path(evaluation.__file__).parent)

    def create(self, task_id, mode, strategy, max_steps, background):
        self.calls.append((task_id, strategy))
        if self.raise_on_create:
            raise RuntimeError('sensitive transport detail must not enter report')
        succeeded = strategy == 'repair'
        return {'id': f'run-{len(self.calls)}', 'task_id': task_id, 'strategy': strategy,
            'status': 'completed' if succeeded else 'failed', 'steps': 2 if succeeded else 1,
            'tokens': 20 if succeeded else 10, 'usage_complete': True, 'elapsed_ms': 5,
            'hidden': {'passed': succeeded}, 'error': None if succeeded else 'visible_tests_failed',
            'source_sha256': 'wrong' if self.bad_run_hash else self.source_hash(),
            'task_sha256': self.tasks[task_id]['sha256'], 'diff': 'example diff\n',
            'protocol_errors': 1, 'context_stats': {'compacted_groups': 2}}


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='patchforge-evaluation-test-')
        self.root = Path(self.temp.name)
        self.engine = FakeEngine(self.root)
        self.docker = patch.object(evaluation.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='sha256:test\n'))
        self.docker.start()
        self.quiet = patch('builtins.print')
        self.quiet.start()

    def tearDown(self):
        self.quiet.stop()
        self.docker.stop()
        self.temp.cleanup()

    def test_invalid_grid_never_starts_an_attempt(self):
        cases = [{'task_ids': []}, {'task_ids': ['missing']}, {'task_ids': ['development-case'] * 2},
                 {'repeats': 0}, {'repeats': 6}, {'repeats': True}, {'max_steps': 0},
                 {'max_model_calls': -1}, {'max_model_calls': 3}, {'mode': 'unknown'},
                 {'task_ids': 'development-case'}, {'seed': False}]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                evaluation.evaluate(self.engine, **options)
        self.assertEqual(self.engine.calls, [])
        self.assertFalse(self.engine.config.run_dir.exists())

    def test_repeat_budget_is_preflighted_for_the_whole_grid(self):
        with self.assertRaisesRegex(ValueError, 'grid_needs_up_to_66_calls_budget_is_65'):
            evaluation.evaluate(self.engine, repeats=3, max_model_calls=65)
        self.assertEqual(self.engine.calls, [])

    def test_changed_task_bytes_rejected_before_any_calls(self):
        path = self.engine.config.task_dir / 'development-case.json'
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'task_file_changed_since_load'):
            evaluation.evaluate(self.engine)
        self.assertEqual(self.engine.calls, [])

    def test_loaded_task_mutation_is_also_rejected(self):
        self.engine.tasks['development-case']['issue'] = 'Changed in memory'
        with self.assertRaisesRegex(ValueError, 'task_file_changed_since_load'):
            evaluation.evaluate(self.engine)
        self.assertEqual(self.engine.calls, [])

    def test_repeated_denominators_splits_pairs_and_artifacts(self):
        report = evaluation.evaluate(self.engine, repeats=2, max_steps=2, max_model_calls=12)
        self.assertTrue(report['complete'])
        self.assertEqual(report['attempts'], 8)
        self.assertEqual(report['planned_attempts'], 8)
        self.assertEqual(report['distinct_tasks'], 2)
        direct, repair = report['summary']
        self.assertEqual((direct['runs'], direct['resolved']), (4, 0))
        self.assertEqual((repair['runs'], repair['resolved']), (4, 4))
        self.assertEqual(report['failure_counts'], {'visible_tests_failed': 4})
        self.assertEqual(report['paired']['repair_only'], 4)
        self.assertEqual(report['paired']['distinct_tasks'], 2)
        self.assertEqual(len(report['by_split']), 2)
        for split in report['by_split']:
            self.assertEqual(split['distinct_tasks'], 1)
            self.assertEqual([summary['runs'] for summary in split['summary']], [2, 2])
        folder = next(self.engine.config.run_dir.glob('evaluation-*Z'))
        self.assertEqual(report['source_sha256'], evaluation._source_hash(folder / 'snapshot' / 'forge'))
        for task_id, loaded in self.engine.tasks.items():
            raw = (folder / 'snapshot' / 'tasks' / (task_id + '.json')).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), loaded['sha256'])
        for row in report['rows']:
            self.assertIn(row['repeat'], (1, 2))
            self.assertEqual(row['protocol_errors'], 1)
            self.assertEqual(row['source_sha256'], report['source_sha256'])
            artifact = folder / row['artifact']
            self.assertTrue((artifact / 'result.json').is_file())
            self.assertEqual((artifact / 'patch.diff').read_text(), 'example diff\n')
        latest = json.loads((self.engine.config.run_dir / 'evaluation-latest.json').read_text(encoding='utf-8'))
        self.assertEqual(latest['rows'], report['rows'])

    def test_attempt_exception_is_retained_and_usage_is_unknown(self):
        self.engine.raise_on_create = True
        report = evaluation.evaluate(self.engine, task_ids=['development-case'])
        self.assertEqual(report['attempts'], 2)
        self.assertTrue(report['complete'])
        self.assertEqual(report['failure_counts'], {'evaluation_attempt_exception': 2})
        self.assertFalse(report['model_call_count_complete'])
        self.assertTrue(all(summary['mean_tokens'] is None for summary in report['summary']))
        self.assertNotIn('sensitive transport', json.dumps(report))

    def test_run_fingerprint_mismatch_cannot_count_as_resolved(self):
        self.engine.bad_run_hash = True
        report = evaluation.evaluate(self.engine, task_ids=['development-case'])
        self.assertTrue(all(not row['resolved'] for row in report['rows']))
        self.assertEqual(report['failure_counts'], {'evaluation_fingerprint_mismatch': 2})

    def test_bad_source_fingerprint_rejected_before_calls(self):
        with patch.object(self.engine, 'source_hash', return_value='wrong'):
            with self.assertRaisesRegex(ValueError, 'evaluation_source_hash_mismatch'):
                evaluation.evaluate(self.engine)
        self.assertEqual(self.engine.calls, [])


if __name__ == '__main__':
    unittest.main()

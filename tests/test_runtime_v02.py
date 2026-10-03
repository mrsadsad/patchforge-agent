"""Behavioral regressions for protocol recovery, atomic edits and version binding."""
import json
import unittest

import test_runtime as runtime_tests
from test_runtime import FakeProvider, FakeSandbox, call, edit, BROKEN, FIXED
from forge.runtime import Engine


class RuntimeV02Tests(unittest.TestCase):
    setUp = runtime_tests.RuntimeBoundaryTests.setUp
    tearDown = runtime_tests.RuntimeBoundaryTests.tearDown
    engine = runtime_tests.RuntimeBoundaryTests.engine

    def test_second_edit_failure_leaves_first_edit_unapplied(self):
        provider = FakeProvider([call('submit_patch', {'edits': [edit(), edit('absent', 'other')], 'summary': 'fix'})])
        state = self.engine(provider=provider).create('fixture', mode='live', strategy='direct', background=False)
        self.assertEqual(state['diff'], '')
        self.assertEqual(state['error'], 'exact_match_count_0_expected_1')

    def test_protocol_failure_is_observed_and_recovered_within_budget(self):
        malformed = call('replace_text', edit())
        malformed['function']['arguments'] = '{"path"::'
        provider = FakeProvider([malformed, call('apply_edits', {'edits': [edit()]}), call('finish', {'summary': 'fixed'})])
        state = self.engine(provider=provider).create('fixture', mode='live', max_steps=3, background=False)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['protocol_errors'], 1)
        self.assertEqual(state['steps'], 3)
        self.assertIn('invalid_arguments', json.dumps(provider.requests[1]))
        self.assertEqual(state['workspace_sha256'], state['visible']['workspace_sha256'])
        self.assertEqual(state['workspace_sha256'], state['hidden']['workspace_sha256'])

    def test_checkpoint_restore_invalidates_tests_and_restores_original(self):
        class RestoreProvider(FakeProvider):
            def complete(self, messages, tools):
                if self.actions[0]['function']['name'] == 'restore_checkpoint':
                    saved = next(json.loads(m['content']) for m in messages if m.get('role') == 'tool'
                                 and '"label": "before change"' in m.get('content', ''))
                    self.actions[0]['function']['arguments'] = json.dumps({'checkpoint_id': saved['id']})
                return super().complete(messages, tools)
        provider = RestoreProvider([call('save_checkpoint', {'label':'before change'}), call('replace_text', edit()),
            call('run_tests', {}), call('restore_checkpoint', {'checkpoint_id':'placeholder'})])
        state = self.engine(provider=provider).create('fixture', mode='live', max_steps=4, background=False)
        self.assertEqual(state['diff'], '')
        self.assertIsNone(state['visible'])
        self.assertEqual(len(state['checkpoints']), 1)
        self.assertEqual(state['workspace_sha256'], state['baseline']['workspace_sha256'])

    def test_external_change_during_test_cannot_be_certified(self):
        sandbox, provider = FakeSandbox(), FakeProvider([])
        engine = self.engine(sandbox=sandbox, provider=provider)
        def mutate():
            identifier = next(iter(engine.states))
            (self.config.run_dir / identifier / 'workspace/solution.py').write_text(FIXED, encoding='utf-8')
        sandbox.before_return = mutate
        state = engine.create('fixture', mode='live', background=False)
        self.assertEqual(state['error'], 'workspace_changed_during_test')
        self.assertEqual(provider.requests, [])

    def test_same_directory_cannot_have_two_schedulers(self):
        self.engine()
        with self.assertRaisesRegex(ValueError, 'run_directory_already_in_use'):
            Engine(self.config, sandbox=FakeSandbox())

    def test_run_preserves_original_snapshot_after_task_catalog_changes(self):
        engine = self.engine()
        state = engine.create('fixture', background=False)
        engine.tasks['fixture']['files']['solution.py'] = 'other = 42\n'
        repo = engine.repository(state['id'])
        self.assertEqual(repo.task['files']['solution.py'], BROKEN)
        self.assertEqual(repo.read('solution.py'), FIXED)

    def test_python_that_parses_but_cannot_compile_is_rejected(self):
        provider = FakeProvider([call('replace_text', edit(new='return 1\n'))])
        state = self.engine(provider=provider).create('fixture', mode='live', max_steps=1, background=False)
        self.assertEqual(state['diff'], '')
        self.assertTrue(any('invalid_python_syntax' in str(e['data']) for e in state['events']))

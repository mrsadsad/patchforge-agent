from __future__ import annotations

from collections import Counter
import hashlib
import json
import random
import shutil
import statistics
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _source_hash(folder):
    digest = hashlib.sha256()
    for file in sorted(folder.glob('*.py')):
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def _atomic(path, value, json_value=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex[:8] + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) if json_value else value,
                         encoding='utf-8', newline='\n')
    temporary.replace(path)


def _summary(rows):
    result = []
    for strategy in ('direct', 'repair'):
        group = [row for row in rows if row['strategy'] == strategy]
        if not group:
            continue
        known = all(row['usage_complete'] and row['tokens'] is not None for row in group)
        resolved = sum(row['resolved'] for row in group)
        result.append({'strategy': strategy, 'runs': len(group), 'resolved': resolved,
            'distinct_tasks': len({row['task_id'] for row in group}), 'resolve_rate': resolved / len(group),
            'mean_steps': statistics.mean(row['steps'] for row in group),
            'mean_tokens': statistics.mean(row['tokens'] for row in group) if known else None,
            'total_tokens': sum(row['tokens'] for row in group) if known else None,
            'mean_elapsed_ms': round(statistics.mean(row['elapsed_ms'] for row in group))})
    return result


def _failures(rows):
    return dict(Counter(row['error'] or row['status'] for row in rows if not row['resolved']))


def _paired(rows):
    pairs, by_task = {}, {}
    for row in rows:
        pairs.setdefault((row['task_id'], row['repeat']), {})[row['strategy']] = row['resolved']
        by_task.setdefault(row['task_id'], {'task_id': row['task_id'], 'split': row['split'],
            'complete_pairs': 0, 'both_resolved': 0, 'direct_only': 0, 'repair_only': 0, 'neither': 0})
    counts = {'both_resolved': 0, 'direct_only': 0, 'repair_only': 0, 'neither': 0, 'pending_pairs': 0}
    for (task_id, _), pair in pairs.items():
        if set(pair) != {'direct', 'repair'}:
            counts['pending_pairs'] += 1
            continue
        outcome = ('both_resolved' if pair['direct'] and pair['repair'] else
                   'direct_only' if pair['direct'] else 'repair_only' if pair['repair'] else 'neither')
        counts[outcome] += 1
        by_task[task_id][outcome] += 1
        by_task[task_id]['complete_pairs'] += 1
    return counts | {'distinct_tasks': len(by_task), 'by_task': list(by_task.values()),
                     'note': '重复运行用于观察同一任务的稳定性，不增加独立任务数，不代表独立泛化样本。'}


def evaluate(engine, task_ids=None, mode='live', max_steps=10, max_model_calls=88, seed=17, repeats=1):
    """Freeze evidence before calls; retain every scheduled direct/repair attempt."""
    if (mode not in {'live', 'demo'} or type(max_steps) is not int or not 1 <= max_steps <= 20
            or type(max_model_calls) is not int or max_model_calls < 0 or type(seed) is not int
            or type(repeats) is not int or not 1 <= repeats <= 5):
        raise ValueError('invalid_evaluation_options')
    selected = list(engine.tasks) if task_ids is None else task_ids
    if (not isinstance(selected, (list, tuple)) or not selected
            or any(not isinstance(task_id, str) for task_id in selected)
            or len(set(selected)) != len(selected) or any(task_id not in engine.tasks for task_id in selected)):
        raise ValueError('invalid_task_selection')
    selected = list(selected)
    if mode == 'live' and not getattr(engine, 'provider', None) and not getattr(engine.config, 'api_key', ''):
        raise ValueError('api_key_not_configured')
    if mode == 'demo' and any(not engine.tasks[task_id].get('demo_edits') for task_id in selected):
        raise ValueError('task_requires_live_model')
    maximum = len(selected) * repeats * (max_steps + 1) if mode == 'live' else 0
    if maximum > max_model_calls:
        raise ValueError(f'grid_needs_up_to_{maximum}_calls_budget_is_{max_model_calls}')

    frozen = {}
    for file in sorted(engine.config.task_dir.glob('*.json')):
        raw = file.read_bytes()
        data = json.loads(raw.decode('utf-8-sig'))
        task_id = data.get('id')
        if task_id not in selected:
            continue
        if task_id in frozen:
            raise ValueError('duplicate_task_file')
        loaded = engine.tasks[task_id]
        digest = hashlib.sha256(raw).hexdigest()
        if digest != loaded.get('sha256') or data != {key: value for key, value in loaded.items() if key != 'sha256'}:
            raise ValueError('task_file_changed_since_load')
        frozen[task_id] = (file.name, raw, digest)
    if set(frozen) != set(selected):
        raise ValueError('task_file_missing')
    source_hash = engine.source_hash()
    if source_hash != _source_hash(Path(__file__).parent):
        raise ValueError('evaluation_source_hash_mismatch')

    instant = datetime.now(timezone.utc)
    stamp = instant.strftime('%Y%m%dT%H%M%S%fZ')
    folder = engine.config.run_dir / ('evaluation-' + stamp)
    snapshot = folder / 'snapshot'
    shutil.copytree(Path(__file__).parent, snapshot / 'forge', ignore=shutil.ignore_patterns('__pycache__'))
    if _source_hash(snapshot / 'forge') != source_hash:
        raise ValueError('source_changed_during_snapshot')
    (snapshot / 'tasks').mkdir()
    for _, (name, raw, _) in frozen.items():
        (snapshot / 'tasks' / name).write_bytes(raw)
    try:
        inspection = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', engine.config.image],
                                    capture_output=True, text=True, timeout=8)
        image_id = inspection.stdout.strip() if inspection.returncode == 0 else 'unknown'
        image_id = image_id or 'unknown'
    except (OSError, subprocess.TimeoutExpired):
        image_id = 'unknown'
    plan = [(task_id, repeat, strategy) for repeat in range(1, repeats + 1)
            for task_id in selected for strategy in ('direct', 'repair')]
    random.Random(seed).shuffle(plan)
    rows = []
    metadata = {'available': True, 'generated_at': instant.isoformat(), 'kind': mode,
        'model': engine.config.model if mode == 'live' else 'scripted-demo', 'task_count': len(selected),
        'distinct_tasks': len(selected), 'repeats': repeats, 'image': engine.config.image, 'image_id': image_id,
        'seed': seed, 'max_steps': max_steps, 'max_model_calls': max_model_calls, 'maximum_model_calls': maximum,
        'planned_attempts': len(plan), 'source_sha256': source_hash,
        'task_sha256': {task_id: frozen[task_id][2] for task_id in selected},
        'split': ','.join(sorted({engine.tasks[task_id]['split'] for task_id in selected})),
        'note': '自建开发集与公开 upstream-replay 按 split 分开统计；公开上游复现不是独立盲测。'
                '重复运行不增加独立任务数。保留测试仅在提交后执行且不回流修复循环。'
                '单轮与多轮共享初始证据和最终验证器，但模型调用预算与成本不同。脚本演示不度量模型能力。'}

    def write_report():
        splits = sorted({row['split'] for row in rows})
        report = metadata | {'attempts': len(rows), 'complete': len(rows) == len(plan),
            'actual_model_calls': sum(row['steps'] for row in rows),
            'model_call_count_complete': all(row.get('call_count_complete', True) for row in rows),
            'summary': _summary(rows), 'rows': list(rows), 'failure_counts': _failures(rows), 'paired': _paired(rows),
            'by_split': [{'split': split, 'distinct_tasks': len({row['task_id'] for row in rows if row['split'] == split}),
                          'summary': _summary([row for row in rows if row['split'] == split]),
                          'failure_counts': _failures([row for row in rows if row['split'] == split])} for split in splits]}
        for path in (folder / 'report.json', engine.config.run_dir / 'evaluation-latest.json'):
            _atomic(path, report)
        return report

    write_report()
    for index, (task_id, repeat, strategy) in enumerate(plan, 1):
        print(f'[{index}/{len(plan)}] {task_id} / repeat {repeat} / {strategy}', flush=True)
        try:
            if engine.source_hash() != source_hash:
                raise ValueError('evaluation_source_changed')
            run = engine.create(task_id, mode=mode, strategy=strategy, max_steps=max_steps, background=False)
        except Exception:
            run = {'id': uuid.uuid4().hex[:16], 'task_id': task_id, 'strategy': strategy, 'status': 'failed',
                'steps': 0, 'tokens': None, 'usage_complete': False, 'elapsed_ms': 0, 'hidden': None,
                'error': 'evaluation_attempt_exception', 'task_sha256': frozen[task_id][2],
                'source_sha256': source_hash, 'diff': '', 'call_count_complete': False}
        run = dict(run)
        if run.get('source_sha256') != source_hash or run.get('task_sha256') != frozen[task_id][2]:
            run.update(status='failed', error='evaluation_fingerprint_mismatch')
        artifact_id = run['id']
        if not isinstance(artifact_id, str) or not artifact_id or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for char in artifact_id):
            artifact_id = uuid.uuid4().hex[:16]
        attempt_folder = folder / 'attempts' / artifact_id
        _atomic(attempt_folder / 'result.json', run)
        _atomic(attempt_folder / 'patch.diff', run.get('diff', ''), json_value=False)
        rows.append({key: run[key] for key in ('task_id', 'strategy', 'status', 'steps', 'tokens', 'usage_complete', 'elapsed_ms')}
            | {'resolved': run['status'] == 'completed' and bool(run.get('hidden') and run['hidden']['passed']),
               'run_id': run['id'], 'artifact': f'attempts/{artifact_id}', 'error': run.get('error'),
               'task_sha256': run['task_sha256'], 'source_sha256': run.get('source_sha256'),
               'repeat': repeat, 'split': engine.tasks[task_id]['split'],
               'protocol_errors': run.get('protocol_errors', 0), 'context_stats': run.get('context_stats', {}),
               'call_count_complete': run.get('call_count_complete', True)})
        report = write_report()
    print(f'Report: {folder / "report.json"}', flush=True)
    return report

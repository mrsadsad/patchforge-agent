from __future__ import annotations

import json
import random
import shutil
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def evaluate(engine, task_ids=None, mode='live', max_steps=10, max_model_calls=88, seed=17):
    """Paired tasks, shuffled order, all attempts retained including failures."""
    task_ids = task_ids or list(engine.tasks)
    if len(set(task_ids)) != len(task_ids) or any(t not in engine.tasks for t in task_ids):
        raise ValueError('invalid_task_selection')
    maximum = len(task_ids) * (max_steps + 1) if mode == 'live' else 0
    if maximum > max_model_calls:
        raise ValueError(f'grid_needs_up_to_{maximum}_calls_budget_is_{max_model_calls}')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = engine.config.run_dir / ('evaluation-' + stamp)
    folder.mkdir(parents=True, exist_ok=False)
    snapshot = folder / 'snapshot'
    shutil.copytree(Path(__file__).parent, snapshot / 'forge', ignore=shutil.ignore_patterns('__pycache__'))
    (snapshot / 'tasks').mkdir()
    for task_id in task_ids:
        (snapshot / 'tasks' / (task_id + '.json')).write_text(
            json.dumps(engine.tasks[task_id], ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        image_id = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', engine.config.image],
                                 capture_output=True, text=True, timeout=8).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        image_id = 'unknown'
    plan = [(t, strategy) for t in task_ids for strategy in ('direct', 'repair')]
    random.Random(seed).shuffle(plan)
    rows = []
    metadata = {'available': True, 'generated_at': stamp, 'kind': mode, 'model':
                engine.config.model if mode == 'live' else 'scripted-demo', 'task_count': len(task_ids),
                'image': engine.config.image, 'image_id': image_id, 'seed': seed, 'max_steps': max_steps,
                'max_model_calls': max_model_calls, 'planned_attempts': len(plan),
                'source_sha256': engine.source_hash(), 'split': 'development',
                'note': '自建小型开发集；单次配对运行，无独立盲测。保留测试只在提交后执行且不向修复循环反馈。'
                        '脚本演示不能度量模型能力。单轮与多轮使用相同初始文件、失败输出及最终验证器；模型预算不同。'}
    for index, (task_id, strategy) in enumerate(plan, 1):
        print(f'[{index}/{len(plan)}] {task_id} / {strategy}', flush=True)
        run = engine.create(task_id, mode=mode, strategy=strategy, max_steps=max_steps, background=False)
        rows.append({k: run[k] for k in ('task_id','strategy','status','steps','tokens','usage_complete','elapsed_ms')}
                    | {'resolved': run['status'] == 'completed' and bool(run['hidden'] and run['hidden']['passed']),
                       'run_id': run['id'], 'error': run['error'], 'task_sha256': run['task_sha256']})
        summary = []
        for strategy_name in ('direct', 'repair'):
            group = [r for r in rows if r['strategy'] == strategy_name]
            if not group:
                continue
            usage_known = all(r['usage_complete'] and r['tokens'] is not None for r in group)
            resolved = sum(r['resolved'] for r in group)
            summary.append({'strategy': strategy_name, 'runs': len(group), 'resolved': resolved,
                'resolve_rate': resolved / len(group), 'mean_steps': statistics.mean(r['steps'] for r in group),
                'mean_tokens': statistics.mean(r['tokens'] for r in group) if usage_known else None,
                'mean_elapsed_ms': round(statistics.mean(r['elapsed_ms'] for r in group))})
        report = metadata | {'attempts': len(rows), 'complete': len(rows) == len(plan),
                            'actual_model_calls': sum(r['steps'] for r in rows), 'summary': summary, 'rows': rows}
        for destination in (folder / 'report.json', engine.config.run_dir / 'evaluation-latest.json'):
            tmp = destination.with_suffix('.tmp')
            tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(destination)
    print(f'Report: {folder / "report.json"}', flush=True)
    return report

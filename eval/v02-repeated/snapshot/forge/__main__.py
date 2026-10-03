from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from .config import Config, load_env
from .evaluation import evaluate
from .repository import load_tasks, public_task
from .runtime import Engine
from .sandbox import docker_status
from .server import serve


def main():
    parser = argparse.ArgumentParser(description='PatchForge execution-grounded code repair agent')
    commands = parser.add_subparsers(dest='command', required=True)
    importer = commands.add_parser('import-task')
    importer.add_argument('--repo', type=Path, required=True)
    importer.add_argument('--issue-file', type=Path, required=True)
    importer.add_argument('--editable', nargs='+', required=True)
    importer.add_argument('--visible-test', nargs='+', required=True)
    importer.add_argument('--hidden-test', nargs='+', required=True)
    importer.add_argument('--id', required=True)
    importer.add_argument('--title', required=True)
    for name in ('serve', 'run', 'evaluate', 'doctor', 'list'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--env-file', type=Path)
        cmd.add_argument('--runs', type=Path)
        if name in {'run', 'evaluate'}:
            cmd.add_argument('--mode', choices=['demo','live'], default='demo' if name == 'run' else 'live')
            cmd.add_argument('--max-steps', type=int, default=10)
        if name == 'serve':
            cmd.add_argument('--port', type=int, default=8770)
        if name == 'run':
            cmd.add_argument('--task', required=True)
            cmd.add_argument('--strategy', choices=['repair','direct'], default='repair')
        if name == 'evaluate':
            cmd.add_argument('--tasks', nargs='+')
            cmd.add_argument('--max-model-calls', type=int, default=88)
            cmd.add_argument('--seed', type=int, default=17)
            cmd.add_argument('--repeats', type=int, default=1)
    args = parser.parse_args()
    if args.command == 'import-task':
        from .importer import import_task
        path = import_task(args.repo, args.issue_file, args.editable, args.visible_test,
                           args.hidden_test, args.id, args.title, Config().task_dir)
        print(path)
        return
    load_env(args.env_file)
    config = Config.from_env(**({'run_dir': args.runs.resolve()} if args.runs else {}))
    if args.command == 'doctor':
        print(json.dumps({**docker_status(config.image), 'image': config.image,
                          'model': config.model, 'api_key_configured': bool(config.api_key)}, indent=2))
        return
    if args.command == 'list':
        print(json.dumps([public_task(t) for t in load_tasks(config.task_dir).values()], ensure_ascii=False, indent=2))
        return
    if not all(docker_status(config.image).values()):
        parser.error(f'Docker and image {config.image} are required. Run: docker pull {config.image}')
    engine = Engine(config)
    if args.command == 'serve':
        serve(engine, args.port)
        return
    try:
        if args.command == 'run':
            state = engine.create(args.task, args.mode, args.strategy, args.max_steps, background=False)
            print(json.dumps({k: state[k] for k in ('id','status','steps','tokens','elapsed_ms','error')}, indent=2))
        else:
            evaluate(engine, args.tasks, args.mode, args.max_steps, args.max_model_calls, args.seed, repeats=args.repeats)
    finally:
        engine.close()


if __name__ == '__main__':
    main()

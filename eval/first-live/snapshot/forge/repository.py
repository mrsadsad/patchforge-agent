from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path, PurePosixPath


class ToolError(ValueError):
    pass


def safe_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ToolError('invalid_path')
    parts = PurePosixPath(name)
    if parts.is_absolute() or '..' in parts.parts or any(p.startswith('.') for p in parts.parts):
        raise ToolError('path_not_allowed')
    root = root.resolve()
    path = root.joinpath(*parts.parts)
    cursor = root
    for part in parts.parts:
        cursor = cursor / part
        if cursor.is_symlink() or (hasattr(cursor, 'is_junction') and cursor.is_junction()):
            raise ToolError('symlink_not_allowed')
    if not path.resolve().is_relative_to(root):
        raise ToolError('path_not_allowed')
    return path


def load_tasks(folder: Path) -> dict[str, dict]:
    tasks = {}
    for path in sorted(folder.glob('*.json')):
        task = json.loads(path.read_text(encoding='utf-8-sig'))
        if task['id'] in tasks:
            raise ValueError('duplicate_task')
        for name in task['files']:
            safe_path(folder, name)
        if not set(task['editable']).issubset(task['files']):
            raise ValueError('invalid_editable_files')
        task['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        tasks[task['id']] = task
    return tasks


def public_task(task: dict) -> dict:
    return {k: task[k] for k in ('id', 'title', 'category', 'difficulty', 'issue', 'split')}


class Repository:
    def __init__(self, root: Path, task: dict, initialize=True):
        self.root = root.resolve()
        self.task = task
        if initialize:
            self.root.mkdir(parents=True, exist_ok=False)
            for name, content in task['files'].items():
                path = safe_path(self.root, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding='utf-8', newline='\n')

    def read(self, path: str) -> str:
        if path not in self.task['files']:
            raise ToolError('file_not_in_task')
        return safe_path(self.root, path).read_text(encoding='utf-8')

    def replace(self, path: str, old: str, new: str) -> dict:
        if path not in self.task['editable']:
            raise ToolError('file_is_read_only')
        if not isinstance(old, str) or not old or not isinstance(new, str) or len(new) > 20000:
            raise ToolError('invalid_replacement')
        content = self.read(path)
        count = content.count(old)
        if count != 1:
            raise ToolError(f'exact_match_count_{count}_expected_1')
        updated = content.replace(old, new, 1)
        if len(updated.encode('utf-8')) > 40000:
            raise ToolError('file_size_limit')
        safe_path(self.root, path).write_text(updated, encoding='utf-8', newline='\n')
        return {'ok': True, 'path': path, 'message': 'Replacement applied. Tests must be rerun.'}

    def search(self, query: str) -> list[dict]:
        if not isinstance(query, str) or not query or len(query) > 200:
            raise ToolError('invalid_query')
        matches = []
        for name in sorted(self.task['files']):
            for number, line in enumerate(self.read(name).splitlines(), 1):
                if query.casefold() in line.casefold():
                    matches.append({'path': name, 'line': number, 'text': line[:300]})
                    if len(matches) >= 30:
                        return matches
        return matches

    def diff(self) -> str:
        return ''.join(''.join(difflib.unified_diff(
            self.task['files'][name].splitlines(keepends=True),
            self.read(name).splitlines(keepends=True),
            fromfile=f'a/{name}', tofile=f'b/{name}',
        )) for name in self.task['editable'])

    def digest(self) -> str:
        return hashlib.sha256(json.dumps({name: self.read(name) for name in self.task['files']},
                            sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def changed(self) -> int:
        return sum(self.read(name) != self.task['files'][name] for name in self.task['editable'])

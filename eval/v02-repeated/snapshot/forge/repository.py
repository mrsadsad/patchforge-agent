from __future__ import annotations

import difflib
import hashlib
import json
import uuid
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
    return {k: task[k] for k in ('id', 'title', 'category', 'difficulty', 'issue', 'split')} | {
        'demo_available': bool(task.get('demo_edits')),
        'file_count': len(task['files']), 'editable': list(task['editable']),
        'provenance': {k: v for k, v in task.get('provenance', {}).items() if k in {
            'kind', 'repository_url', 'base_commit', 'fix_commit', 'source_url', 'license', 'reproduction_note'}}}


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
        return self.apply_edits([{'path': path, 'old': old, 'new': new}]) | {'path': path}

    def apply_edits(self, edits: list[dict]) -> dict:
        """Validate the entire batch first; roll back ordinary write failures.

        This is logical all-or-nothing editing, not crash-atomic multi-file IO.
        """
        if not isinstance(edits, list) or not 1 <= len(edits) <= 10:
            raise ToolError('invalid_edits_count')
        staged = {}
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {'path', 'old', 'new'}:
                raise ToolError('invalid_edit')
            path, old, new = edit['path'], edit['old'], edit['new']
            if not isinstance(path, str) or path not in self.task['editable']:
                raise ToolError('file_is_read_only')
            if not isinstance(old, str) or not old or not isinstance(new, str) or len(new) > 20000:
                raise ToolError('invalid_replacement')
            source = staged.get(path, self.read(path))
            count = source.count(old)
            if count != 1:
                raise ToolError(f'exact_match_count_{count}_expected_1')
            staged[path] = source.replace(old, new, 1)
        for path, content in staged.items():
            self._validate_source(path, content)
        self._write_batch(staged)
        return {'ok': True, 'paths': list(staged), 'workspace_sha256': self.digest(),
                'message': 'All edits applied. Earlier test results are stale; rerun tests.'}

    def _validate_source(self, path, content):
        if not isinstance(content, str) or len(content.encode('utf-8')) > 40000:
            raise ToolError('file_size_limit')
        if path.endswith('.py'):
            try:
                compile(content, path, 'exec', dont_inherit=True)
            except (SyntaxError, ValueError) as error:
                raise ToolError(f'invalid_python_syntax_line_{getattr(error, "lineno", 0)}') from None

    def _write_batch(self, staged):
        originals = {name: self.read(name) for name in staged}
        try:
            for name, content in staged.items():
                safe_path(self.root, name).write_text(content, encoding='utf-8', newline='\n')
        except OSError:
            for name, content in originals.items():
                safe_path(self.root, name).write_text(content, encoding='utf-8', newline='\n')
            raise ToolError('write_failed_rolled_back') from None

    def read_range(self, path, start_line=1, end_line=None):
        if type(start_line) is not int or start_line < 1 or (end_line is not None and
                (type(end_line) is not int or end_line < start_line)):
            raise ToolError('invalid_line_range')
        lines = self.read(path).splitlines(keepends=True)
        if start_line > max(1, len(lines)):
            raise ToolError('line_range_out_of_bounds')
        stop = min(end_line or start_line + 119, start_line + 199, len(lines))
        content = ''.join(lines[start_line - 1:stop])
        if len(content) > 14000:
            raise ToolError('range_too_large_request_fewer_lines')
        return {'ok': True, 'path': path, 'start_line': start_line, 'end_line': stop,
                'total_lines': len(lines), 'content': content, 'truncated': stop < len(lines)}

    def save_checkpoint(self, label):
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise ToolError('invalid_checkpoint_label')
        folder = self.root.parent / 'checkpoints'
        folder.mkdir(exist_ok=True)
        if len(list(folder.glob('*.json'))) >= 10:
            raise ToolError('checkpoint_limit')
        identifier = uuid.uuid4().hex[:12]
        snapshot = {'id': identifier, 'label': label,
                    'files': {p: self.read(p) for p in self.task['editable']},
                    'workspace_sha256': self.digest()}
        (folder / (identifier + '.json')).write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        return {k: snapshot[k] for k in ('id', 'label', 'workspace_sha256')}

    def restore_checkpoint(self, checkpoint_id):
        if (not isinstance(checkpoint_id, str) or len(checkpoint_id) != 12
                or any(c not in '0123456789abcdef' for c in checkpoint_id)):
            raise ToolError('invalid_checkpoint_id')
        path = self.root.parent / 'checkpoints' / (checkpoint_id + '.json')
        if not path.is_file() or path.is_symlink():
            raise ToolError('checkpoint_not_found')
        snapshot = json.loads(path.read_text(encoding='utf-8'))
        if set(snapshot['files']) != set(self.task['editable']):
            raise ToolError('checkpoint_files_mismatch')
        for name, content in snapshot['files'].items():
            self._validate_source(name, content)
        self._write_batch(snapshot['files'])
        return {'ok': True, 'checkpoint_id': checkpoint_id, 'workspace_sha256': self.digest(),
                'message': 'Restored editable files. Tests must be rerun.'}

    def files(self):
        return {'workspace_sha256': self.digest(), 'files': [
            {'path': name, 'editable': name in self.task['editable'],
             'changed': self.read(name) != original, 'lines': len(self.read(name).splitlines()),
             'sha256': hashlib.sha256(self.read(name).encode()).hexdigest()}
            for name, original in sorted(self.task['files'].items())]}

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

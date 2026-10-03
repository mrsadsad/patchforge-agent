"""Explicitly allowlisted, reproducible review bundles; never zip run directories."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

from .repository import Repository, public_task


def review_bundle(state: dict, repo: Repository) -> bytes:
    payloads = {'result.json': json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8'),
                'task.json': json.dumps(public_task(repo.task), ensure_ascii=False, indent=2).encode('utf-8'),
                'patch.diff': state['diff'].encode('utf-8')}
    for path, original in repo.task['files'].items():
        payloads['before/' + path] = original.encode('utf-8')
        payloads['after/' + path] = repo.read(path).encode('utf-8')
    manifest = {'format': 'patchforge-review-v1', 'run_id': state['id'],
                'workspace_sha256': repo.digest(), 'task_sha256': state['task_sha256'],
                'source_sha256': state['source_sha256'],
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payloads.items())}}
    payloads['manifest.json'] = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2).encode('utf-8')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payloads.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue()

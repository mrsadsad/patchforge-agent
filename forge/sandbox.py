"""Disposable non-networked containers. Never execute generated Python on the host."""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

# Kept outside the editable workspace. Not a defense against malicious Python
# monkeypatching its own test process; see docs/ARCHITECTURE.md.
HARNESS = '''import contextlib, io, json, sys, unittest
sys.path.insert(0, '/workspace')
stream = io.StringIO()
with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
    suite = unittest.defaultTestLoader.discover('/checks', pattern='test_*.py')
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
payload = {'total': result.testsRun, 'failures': len(result.failures),
           'errors': len(result.errors), 'skipped': len(result.skipped),
           'passed': result.wasSuccessful() and result.testsRun > 0 and not result.skipped,
           'output': stream.getvalue()[-14000:]}
print('PATCHFORGE_RESULT=' + json.dumps(payload))
'''


class SandboxError(RuntimeError):
    pass


def docker_status(image: str) -> dict:
    try:
        ready = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                               capture_output=True, timeout=8).returncode == 0
        present = ready and subprocess.run(['docker', 'image', 'inspect', image],
                            capture_output=True, timeout=8).returncode == 0
        return {'docker_available': ready, 'image_available': bool(present)}
    except (OSError, subprocess.TimeoutExpired):
        return {'docker_available': False, 'image_available': False}


class Sandbox:
    def __init__(self, image='python:3.12-slim', timeout=25):
        self.timeout = timeout
        try:
            result = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image],
                                    capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            raise SandboxError('docker_image_unavailable') from None
        identifier = result.stdout.strip()
        if result.returncode or not identifier.startswith('sha256:'):
            raise SandboxError('docker_image_unavailable')
        # Pin once for this runtime so a mutable tag cannot change between suites.
        self.image = identifier

    def command(self, root: Path, checks: Path, harness: Path, name: str) -> list[str]:
        return ['docker', 'run', '--rm', '--pull=never', '--name', name,
                '--network=none', '--read-only', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', '--pids-limit=64',
                '--memory=192m', '--memory-swap=192m', '--cpus=1',
                '--user=65534:65534', '--log-driver=none',
                '--ulimit', 'nofile=128:128', '--ulimit', 'fsize=1048576:1048576',
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                '--mount', f'type=bind,source={root.resolve()},target=/workspace,readonly',
                '--mount', f'type=bind,source={checks.resolve()},target=/checks,readonly',
                '--mount', f'type=bind,source={harness.resolve()},target=/harness,readonly',
                '--workdir=/workspace', self.image, 'python', '-I', '-B', '/harness/verify.py']

    def run(self, root: Path, tests: dict[str, str], cancel=None) -> dict:
        start = time.monotonic()
        name = 'patchforge-' + uuid.uuid4().hex[:16]
        with tempfile.TemporaryDirectory(prefix='patchforge-checks-') as tmp:
            folder = Path(tmp)
            checks, harness = folder / 'checks', folder / 'harness'
            checks.mkdir(); harness.mkdir()
            for filename, source in tests.items():
                if not filename.startswith('test_') or '/' in filename or '\\' in filename:
                    raise SandboxError('invalid_test_filename')
                (checks / filename).write_text(source, encoding='utf-8', newline='\n')
            (harness / 'verify.py').write_text(HARNESS, encoding='utf-8', newline='\n')
            with tempfile.TemporaryFile() as output:
                try:
                    process = subprocess.Popen(self.command(root, checks, harness, name),
                                               stdout=output, stderr=output)
                except OSError:
                    raise SandboxError('docker_unavailable') from None
                stopped = None
                try:
                    while process.poll() is None:
                        if cancel and cancel():
                            stopped = 'cancelled'; break
                        if time.monotonic() - start > self.timeout:
                            stopped = 'test_timeout'; break
                        if output.tell() > 1_000_000:
                            stopped = 'test_output_limit'; break
                        time.sleep(.08)
                finally:
                    if process.poll() is None:
                        try:
                            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=8)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                        process.kill()
                        process.wait(timeout=5)
                        # The daemon may have accepted container creation after
                        # the first removal request. Clean up again after CLI exit.
                        for _ in range(2):
                            try:
                                subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=8)
                            except (OSError, subprocess.TimeoutExpired):
                                pass
                            time.sleep(.1)
                output.seek(0)
                text = output.read(1_000_000).decode('utf-8', errors='replace')
        elapsed = round((time.monotonic() - start) * 1000)
        if stopped:
            return {'passed': False, 'total': 0, 'failures': 0, 'errors': 1,
                    'output': stopped, 'elapsed_ms': elapsed, 'error': stopped}
        marker = 'PATCHFORGE_RESULT='
        lines = [line[len(marker):] for line in text.splitlines() if line.startswith(marker)]
        if process.returncode != 0 or len(lines) != 1:
            raise SandboxError('sandbox_execution_failed')
        try:
            result = json.loads(lines[0])
            if any(type(result[k]) is not int or result[k] < 0 for k in ('total','failures','errors','skipped')):
                raise ValueError
            if type(result['passed']) is not bool or not isinstance(result['output'], str):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise SandboxError('invalid_test_result') from None
        return {**result, 'elapsed_ms': elapsed, 'image': self.image}

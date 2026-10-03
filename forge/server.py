from __future__ import annotations

import json
import hashlib
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from . import __version__
from .repository import public_task, Repository, ToolError
from .artifacts import review_bundle
from .runtime import Engine
from .sandbox import docker_status


def make_server(engine: Engine, port=8770):
    token = secrets.token_urlsafe(32)
    health = docker_status(engine.config.image)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'PatchForge'

        def log_message(self, format, *args):
            # Avoid logging query strings or user data into terminal output.
            pass

        def send(self, code, value, content_type='application/json; charset=utf-8', download=False):
            body = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False).encode('utf-8') if content_type.startswith('application/json')
                    else value.encode('utf-8'))
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if download:
                self.send_header('Content-Disposition', 'attachment; filename="' +
                                 ('patchforge-review.zip' if content_type == 'application/zip' else 'patch.diff') + '"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def valid_host(self):
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            return self.headers.get('Host') in allowed

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {'error': 'invalid_host'})
            path = urlsplit(self.path).path
            if path == '/':
                return self.send(200, (Path(__file__).parent / 'web/index.html').read_text(encoding='utf-8'),
                                 'text/html; charset=utf-8')
            if path == '/api/config':
                return self.send(200, {'csrf_token': token, 'model': engine.config.model, 'version': __version__,
                    'live_available': bool(engine.config.api_key) and all(health.values()), **health,
                    'tasks': [public_task(t) for t in engine.tasks.values()]})
            if path == '/api/runs':
                return self.send(200, {'runs': engine.list()})
            if path == '/api/evaluation':
                report = engine.config.run_dir / 'evaluation-latest.json'
                return self.send(200, json.loads(report.read_text(encoding='utf-8')) if report.exists() else {'available': False})
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[:2] == ['api', 'tasks'] and parts[3] in {'files','file'}:
                task = engine.tasks.get(parts[2])
                if task is None:
                    return self.send(404, {'error': 'task_not_found'})
                if parts[3] == 'files':
                    return self.send(200, {'files': [{'path': name, 'editable': name in task['editable'],
                        'changed': False, 'lines': len(content.splitlines()),
                        'sha256': hashlib.sha256(content.encode()).hexdigest()}
                        for name, content in sorted(task['files'].items())], 'workspace_sha256': None})
                query = parse_qs(urlsplit(self.path).query)
                name = query.get('path', [''])[0]
                if name not in task['files']:
                    return self.send(404, {'error': 'file_not_found'})
                content = task['files'][name]
                return self.send(200, {'path': name, 'original': content, 'content': content,
                    'editable': name in task['editable'], 'sha256': hashlib.sha256(content.encode()).hexdigest()})
            if len(parts) in (3, 4) and parts[:2] == ['api', 'runs']:
                try:
                    state = engine.get(parts[2])
                except KeyError:
                    return self.send(404, {'error': 'run_not_found'})
                if len(parts) == 3:
                    return self.send(200, state)
                if parts[3] == 'patch':
                    return self.send(200, state['diff'], 'text/plain; charset=utf-8', download=True)
                if parts[3] in {'files', 'file', 'bundle'}:
                    try:
                        repo = engine.repository(state['id'])
                        if parts[3] == 'files':
                            return self.send(200, repo.files())
                        if parts[3] == 'bundle':
                            if state['status'] in {'running', 'queued'}:
                                return self.send(409, {'error': 'run_still_active'})
                            if state.get('workspace_sha256') and state['workspace_sha256'] != repo.digest():
                                return self.send(409, {'error': 'workspace_changed_since_completion'})
                            return self.send(200, review_bundle(state, repo), 'application/zip', download=True)
                        name = parse_qs(urlsplit(self.path).query).get('path', [''])[0]
                        content = repo.read(name)
                        return self.send(200, {'path': name, 'original': repo.task['files'][name], 'content': content,
                            'editable': name in repo.task['editable'], 'sha256': hashlib.sha256(content.encode()).hexdigest()})
                    except (ToolError, OSError):
                        return self.send(404, {'error': 'file_not_available'})
            return self.send(404, {'error': 'not_found'})

        def do_POST(self):
            expected_origin = f'http://{self.headers.get("Host", "")}'
            if (not self.valid_host() or self.headers.get('X-CSRF-Token') != token
                    or self.headers.get('Origin', expected_origin) != expected_origin
                    or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
                return self.send(403, {'error': 'invalid_origin_or_token'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 8192:
                    return self.send(413, {'error': 'invalid_body_size'})
                self.connection.settimeout(10)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('body_must_be_object')
                path = urlsplit(self.path).path
                if path == '/api/runs':
                    if not all(health.values()):
                        return self.send(409, {'error': 'docker_or_image_unavailable'})
                    if set(data) - {'task_id', 'mode', 'strategy', 'max_steps'}:
                        raise ValueError('unknown_request_fields')
                    return self.send(201, engine.create(**data))
                parts = path.strip('/').split('/')
                if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'cancel':
                    return self.send(200, engine.cancel(parts[2]))
                return self.send(404, {'error': 'not_found'})
            except KeyError:
                return self.send(404, {'error': 'run_not_found'})
            except (ValueError, TypeError) as error:
                code = str(error) if isinstance(error, ValueError) and str(error).replace('_', '').isalnum() else 'invalid_request'
                return self.send(400, {'error': code})
            except (OSError, TimeoutError):
                return self.send(408, {'error': 'request_timeout'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def serve(engine, port=8770):
    server = make_server(engine, port)
    print(f'PatchForge: http://127.0.0.1:{server.server_port}', flush=True)
    print('Mode is selected per run. Demo = scripted reference patch; live = actual model.', flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.close()

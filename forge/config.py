from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """Read only the explicitly selected credential file, without logging values."""
    path = path or ROOT / '.env'
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key in {'PATCHFORGE_API_KEY', 'PATCHFORGE_BASE_URL', 'PATCHFORGE_MODEL',
                   'PATCHFORGE_IMAGE', 'DEEPSEEK_API_KEY'}:
            os.environ.setdefault(key, value.strip().strip('\"\''))


@dataclass(frozen=True)
class Config:
    api_key: str = ''
    base_url: str = 'https://api.deepseek.com'
    model: str = 'deepseek-flash'
    image: str = 'python:3.12-slim'
    run_dir: Path = ROOT / 'runs'
    task_dir: Path = ROOT / 'tasks'
    test_timeout: int = 25

    @classmethod
    def from_env(cls, **kwargs):
        return cls(api_key=os.getenv('PATCHFORGE_API_KEY') or os.getenv('DEEPSEEK_API_KEY', ''),
                   base_url=os.getenv('PATCHFORGE_BASE_URL', cls.base_url),
                   model=os.getenv('PATCHFORGE_MODEL', cls.model),
                   image=os.getenv('PATCHFORGE_IMAGE', cls.image), **kwargs)

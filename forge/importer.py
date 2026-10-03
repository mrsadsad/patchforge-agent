"""Create bounded local-repository tasks without importing or executing their code."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


MAX_FILES = 60
MAX_TOTAL_BYTES = 300_000
MAX_FILE_BYTES = 40_000
MAX_ISSUE_BYTES = 8_000
MAX_SCAN_ENTRIES = 5_000
README_NAMES = {"readme", "readme.md", "readme.rst", "readme.txt"}
EXCLUDED_DIRS = {"venv", "env", "node_modules", "__pycache__", "site-packages", "dist", "build", "tests", "test"}
SECRET_NAMES = {"secrets.py", "secret.py", "credentials.py", "credential.py", "tokens.py", "passwords.py"}
SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
TEST_NAME = re.compile(r"test_[a-zA-Z0-9_]+\.py\Z")
SECRET_CONTENT = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    r"|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"
    r"|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret[_-]?key)\s*[:=]\s*[\"'][^\"'\r\n]{4,}[\"']"
    r"|https?://[^/\s:@]+:[^/\s@]+@",
    re.IGNORECASE,
)


class ImportTaskError(ValueError):
    """A validation failure; messages intentionally contain no file contents."""


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _no_link_ancestors(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        if (part.exists() or part.is_symlink()) and _linked(part):
            raise ImportTaskError("links_not_allowed")
    return path


def _relative(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise ImportTaskError("invalid_relative_path")
    parts = name.split("/")
    if any(not part or part.startswith(".") or part.endswith((".", " ")) for part in parts):
        raise ImportTaskError("invalid_relative_path")
    if PurePosixPath(name).is_absolute():
        raise ImportTaskError("invalid_relative_path")
    return name


def _blocked(name: str) -> bool:
    lowered = name.casefold()
    return lowered.startswith(".") or lowered in SECRET_NAMES or lowered.startswith(("secret.", "credentials.", "credential."))


def _selected(root: Path, name: str) -> Path:
    name = _relative(name)
    parts = PurePosixPath(name).parts
    if any(_blocked(part) or (part.casefold() in EXCLUDED_DIRS - {"tests", "test"}) for part in parts):
        raise ImportTaskError("selected_path_excluded")
    path = _no_link_ancestors(root.joinpath(*parts))
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ImportTaskError("selected_file_missing")
    if not path.resolve().is_relative_to(root):
        raise ImportTaskError("path_outside_repository")
    return path


def _read(path: Path, limit: int) -> tuple[str, int]:
    if _linked(path) or not stat.S_ISREG(path.stat().st_mode):
        raise ImportTaskError("regular_file_required")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ImportTaskError("issue_size_limit" if limit == MAX_ISSUE_BYTES else "file_size_limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportTaskError("utf8_text_required") from None
    if any(ord(character) < 32 and character not in "\t\r\n" for character in text):
        raise ImportTaskError("plain_text_required")
    return text.replace("\r\n", "\n").replace("\r", "\n"), len(raw)


def _test_source(root: Path, name: str) -> tuple[Path, str, int]:
    path = _selected(root, name)
    if not TEST_NAME.fullmatch(path.name):
        raise ImportTaskError("test_filename_required")
    content, size = _read(path, MAX_FILE_BYTES)
    if not content.strip():
        raise ImportTaskError("empty_test_file")
    if SECRET_CONTENT.search(content):
        raise ImportTaskError("secret_content_not_allowed")
    return path, content, size


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def import_task(repo: Path, issue_file: Path, editable: list[str], visible_tests: list[str],
                hidden_tests: list[str], task_id: str, title: str, task_dir: Path) -> Path:
    """Write a new task JSON exclusively; never execute code or overwrite a task.

    Selected paths use repository-relative forward slashes. The hidden test file
    stays outside the model-visible snapshot; visible tests become root modules.
    Limits count source, selected visible tests and the one hidden test together.
    """
    if not isinstance(task_id, str) or len(task_id) > 64 or not SLUG.fullmatch(task_id):
        raise ImportTaskError("invalid_task_id")
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or any(ord(c) < 32 for c in title):
        raise ImportTaskError("invalid_title")
    if not isinstance(editable, list) or not editable:
        raise ImportTaskError("editable_files_required")
    if not isinstance(visible_tests, list) or not visible_tests:
        raise ImportTaskError("visible_tests_required")
    if not isinstance(hidden_tests, list) or len(hidden_tests) != 1:
        raise ImportTaskError("exactly_one_hidden_test_required")
    selected_names = [_relative(name) for name in [*editable, *visible_tests, *hidden_tests]]
    if len({name.casefold() for name in selected_names}) != len(selected_names):
        raise ImportTaskError("duplicate_or_overlapping_selected_paths")

    root = _no_link_ancestors(Path(repo))
    if not root.is_dir():
        raise ImportTaskError("repository_directory_required")
    root = root.resolve()
    issue_path = _no_link_ancestors(Path(issue_file))
    if not issue_path.is_file() or _blocked(issue_path.name):
        raise ImportTaskError("issue_file_required")
    issue, _ = _read(issue_path, MAX_ISSUE_BYTES)
    if not issue.strip():
        raise ImportTaskError("empty_issue")
    if SECRET_CONTENT.search(issue):
        raise ImportTaskError("secret_content_not_allowed")

    editable_paths = {name: _selected(root, name) for name in editable}
    for name in editable_paths:
        parts = PurePosixPath(name).parts
        if (not name.endswith(".py") or any(part.casefold() in {"tests", "test"} for part in parts[:-1])
                or parts[-1].startswith("test_") or parts[-1].endswith("_test.py")):
            raise ImportTaskError("editable_must_be_python_source")

    visible = [_test_source(root, name) for name in visible_tests]
    hidden_path, hidden_content, hidden_size = _test_source(root, hidden_tests[0])
    test_names = [path.name.casefold() for path, _, _ in visible] + [hidden_path.name.casefold()]
    if len(set(test_names)) != len(test_names) or "test_hidden.py" in test_names[:-1]:
        raise ImportTaskError("test_basename_collision")
    test_identities = {_identity(path) for path, _, _ in visible} | {_identity(hidden_path)}
    if len(test_identities) != len(visible) + 1:
        raise ImportTaskError("test_files_must_be_distinct")
    if any(_identity(path) in test_identities for path in editable_paths.values()):
        raise ImportTaskError("editable_test_alias")

    files = {path.name: content for path, content, _ in visible}
    total_bytes = hidden_size + sum(size for _, _, size in visible)
    file_count = len(visible) + 1

    def check_limits():
        if file_count > MAX_FILES:
            raise ImportTaskError("file_count_limit")
        if total_bytes > MAX_TOTAL_BYTES:
            raise ImportTaskError("total_size_limit")

    check_limits()
    directories = [root]
    scanned = 0
    while directories:
        directory = directories.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_SCAN_ENTRIES:
                    raise ImportTaskError("scan_entry_limit")
                path = Path(entry.path)
                if _blocked(entry.name) or _linked(path):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() not in EXCLUDED_DIRS:
                        directories.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                name = path.relative_to(root).as_posix()
                if path.suffix != ".py" and not (directory == root and entry.name.casefold() in README_NAMES):
                    continue
                if entry.name.startswith("test_") or entry.name.endswith("_test.py") or _identity(path) in test_identities:
                    continue
                _relative(name)
                content, size = _read(path, MAX_FILE_BYTES)
                if SECRET_CONTENT.search(content):
                    if name in editable_paths:
                        raise ImportTaskError("secret_content_not_allowed")
                    continue
                if any(existing.casefold() == name.casefold() for existing in files):
                    raise ImportTaskError("snapshot_path_collision")
                files[name] = content
                total_bytes += size
                file_count += 1
                check_limits()
    if not set(editable).issubset(files):
        raise ImportTaskError("editable_file_excluded")

    task = {
        "id": task_id, "title": title.strip(), "category": "Imported repository",
        "difficulty": "medium", "split": "imported", "issue": issue,
        "files": dict(sorted(files.items())), "editable": editable,
        "hidden_tests": hidden_content, "demo_edits": [], "demo_available": False,
        "provenance": {"kind": "local_repository_import", "file_count": file_count,
                       "total_bytes": total_bytes, "reference_patch_provided": False},
    }
    destination = _no_link_ancestors(Path(task_dir))
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"{task_id}.json"
    try:
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(task, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        raise ImportTaskError("task_already_exists") from None
    return output

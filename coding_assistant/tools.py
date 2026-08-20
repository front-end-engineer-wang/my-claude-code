"""Filesystem, shell, and session todo tools."""

import atexit
import ast
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .config import WORKDIR
from .workspace import current_workdir
from .tasks import CURRENT_TODOS, assignment_cwd

# -- Basic Tools --


def safe_path(path: str, cwd: Path | None = None) -> Path:
    base = (cwd or WORKDIR).resolve()
    resolved = (base / path).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved


def _read_text_file(path: Path) -> str:
    """Read repository text as UTF-8, with a Windows legacy fallback."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gb18030")


def _matches_glob(path: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    normalized = path.replace("\\", "/")
    return (
        fnmatch.fnmatch(normalized, pattern)
        or Path(normalized).match(pattern)
        or (pattern.startswith("**/")
            and fnmatch.fnmatch(normalized, pattern[3:]))
    )


def _search_with_rg(
    executable: str,
    query: str,
    base: Path,
    glob: str | None,
    case_sensitive: bool,
    max_results: int,
) -> tuple[list[str], bool]:
    args = [
        executable,
        "--line-number",
        "--column",
        "--no-heading",
        "--color",
        "never",
        "--fixed-strings",
    ]
    if not case_sensitive:
        args.append("--ignore-case")
    if glob:
        args.extend(["--glob", glob])
    args.extend(["--", query, "."])

    process = subprocess.Popen(
        args,
        cwd=base,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    matches: list[str] = []
    limit_reached = False
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            if line:
                matches.append(line.removeprefix(".\\").removeprefix("./"))
            if len(matches) >= max_results:
                limit_reached = True
                process.terminate()
                break
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        stderr = process.stderr.read().strip() if process.stderr else ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
    if process.returncode not in {0, 1, -15, 1} and not limit_reached:
        raise RuntimeError(stderr or f"rg exited with status {process.returncode}")
    return matches[:max_results], limit_reached


def _search_with_python(
    query: str,
    base: Path,
    glob: str | None,
    case_sensitive: bool,
    max_results: int,
) -> tuple[list[str], bool]:
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(query), flags)
    matches: list[str] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        try:
            if not path.resolve().is_relative_to(base):
                continue
        except OSError:
            continue
        relative = path.relative_to(base)
        if any(part.startswith(".") or part == "__pycache__"
               for part in relative.parts):
            continue
        relative_text = relative.as_posix()
        if not _matches_glob(relative_text, glob):
            continue
        try:
            data = path.read_bytes()
            if b"\0" in data[:8192]:
                continue
            text = _read_text_file(path)
        except (OSError, UnicodeError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if not match:
                continue
            matches.append(
                f"{relative_text}:{line_number}:{match.start() + 1}:{line}"
            )
            if len(matches) >= max_results:
                return matches, True
    return matches, False


def run_search_text(
    query: str,
    glob: str | None = None,
    case_sensitive: bool = False,
    max_results: int = 100,
    cwd: Path | None = None,
) -> str:
    """Search workspace text with ripgrep, falling back to Python."""
    if not isinstance(query, str) or not query:
        return "Error: query must be a non-empty string"
    if glob is not None and (not isinstance(glob, str) or not glob):
        return "Error: glob must be a non-empty string when provided"
    try:
        limit = int(max_results)
    except (TypeError, ValueError):
        return "Error: max_results must be an integer"
    if limit < 1 or limit > 500:
        return "Error: max_results must be between 1 and 500"

    base = (cwd or WORKDIR).resolve()
    executable = shutil.which("rg")
    backend = "python"
    if executable:
        try:
            matches, truncated = _search_with_rg(
                executable, query, base, glob, case_sensitive, limit
            )
            backend = "rg"
        except (OSError, RuntimeError, subprocess.SubprocessError):
            matches, truncated = _search_with_python(
                query, base, glob, case_sensitive, limit
            )
    else:
        matches, truncated = _search_with_python(
            query, base, glob, case_sensitive, limit
        )

    header = f"Search backend: {backend}; matches: {len(matches)}"
    if truncated:
        header += f"; limit reached: {limit}"
    return header + ("\n" + "\n".join(matches) if matches else "\n(no matches)")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_patch_temp(path: Path, content: str, mode: int) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix=f".{path.name}.",
        suffix=".patch.tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(mode))
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_patch_bytes_temp(path: Path, content: bytes, mode: int) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        suffix=".rollback.tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(mode))
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def run_apply_patch(patches: list[dict], cwd: Path | None = None) -> str:
    """Validate and transactionally apply exact-context hunks to many files."""
    if not isinstance(patches, list) or not patches:
        return "Error: patches must be a non-empty list"

    base = (cwd or WORKDIR).resolve()
    staged: list[tuple[Path, bytes, str, int, int]] = []
    seen: set[Path] = set()
    total_hunks = 0

    try:
        for file_index, file_patch in enumerate(patches):
            if not isinstance(file_patch, dict):
                raise ValueError(f"patches[{file_index}] must be an object")
            raw_path = file_patch.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError(f"patches[{file_index}].path must be non-empty")
            path = safe_path(raw_path, base)
            if path in seen:
                raise ValueError(f"duplicate patch path: {raw_path}")
            seen.add(path)
            if not path.is_file():
                raise ValueError(f"file not found: {raw_path}")

            original_bytes = path.read_bytes()
            expected_sha256 = file_patch.get("expected_sha256")
            if expected_sha256 is not None:
                if not isinstance(expected_sha256, str):
                    raise ValueError(
                        f"patches[{file_index}].expected_sha256 must be a string"
                    )
                actual_sha256 = _sha256(original_bytes)
                if actual_sha256.casefold() != expected_sha256.casefold():
                    raise ValueError(
                        f"stale file {raw_path}: SHA-256 is {actual_sha256}"
                    )

            text = _read_text_file(path)
            hunks = file_patch.get("hunks")
            if not isinstance(hunks, list) or not hunks:
                raise ValueError(f"patches[{file_index}].hunks must be non-empty")
            for hunk_index, hunk in enumerate(hunks):
                if not isinstance(hunk, dict):
                    raise ValueError(
                        f"patches[{file_index}].hunks[{hunk_index}] must be an object"
                    )
                old_text = hunk.get("old_text")
                new_text = hunk.get("new_text")
                if not isinstance(old_text, str) or not old_text:
                    raise ValueError(
                        f"patches[{file_index}].hunks[{hunk_index}].old_text "
                        "must be non-empty"
                    )
                if not isinstance(new_text, str):
                    raise ValueError(
                        f"patches[{file_index}].hunks[{hunk_index}].new_text "
                        "must be a string"
                    )
                if old_text == new_text:
                    raise ValueError(
                        f"patches[{file_index}].hunks[{hunk_index}] makes no change"
                    )
                expected = hunk.get("expected_occurrences", 1)
                if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
                    raise ValueError(
                        f"patches[{file_index}].hunks[{hunk_index}]."
                        "expected_occurrences must be a positive integer"
                    )
                actual = text.count(old_text)
                if actual != expected:
                    raise ValueError(
                        f"context mismatch in {raw_path} hunk {hunk_index}: "
                        f"expected {expected} occurrence(s), found {actual}"
                    )
                text = text.replace(old_text, new_text, expected)
                total_hunks += 1
            staged.append(
                (path, original_bytes, text, path.stat().st_mode, len(hunks))
            )
    except (OSError, UnicodeError, ValueError) as exc:
        return f"Error: patch validation failed: {exc}"

    temporary_files: dict[Path, Path] = {}
    replaced: list[tuple[Path, bytes, int]] = []
    try:
        for path, _, content, mode, _ in staged:
            temporary_files[path] = _write_patch_temp(path, content, mode)
        for path, original, _, mode, _ in staged:
            os.replace(temporary_files.pop(path), path)
            replaced.append((path, original, mode))
    except Exception as exc:
        rollback_errors = []
        for path, original, mode in reversed(replaced):
            try:
                rollback = _write_patch_bytes_temp(path, original, mode)
                os.replace(rollback, path)
            except Exception as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        detail = f"Error: patch commit failed: {type(exc).__name__}: {exc}"
        if rollback_errors:
            detail += "; rollback failed for " + ", ".join(rollback_errors)
        return detail
    finally:
        for temporary in temporary_files.values():
            temporary.unlink(missing_ok=True)

    files = ", ".join(path.relative_to(base).as_posix() for path, *_ in staged)
    return f"Patched {len(staged)} file(s), {total_hunks} hunk(s): {files}"


SEARCH_TEXT_TOOL = {
    "name": "search_text",
    "description": "Search workspace text with rg when available.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "glob": {"type": "string"},
            "case_sensitive": {"type": "boolean", "default": False},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


APPLY_PATCH_TOOL = {
    "name": "apply_patch",
    "description": (
        "Apply exact-context hunks to multiple existing files atomically. "
        "Every hunk must match its expected occurrence count."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "patches": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "expected_sha256": {"type": "string"},
                        "hunks": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old_text": {"type": "string"},
                                    "new_text": {"type": "string"},
                                    "expected_occurrences": {
                                        "type": "integer", "minimum": 1,
                                        "default": 1,
                                    },
                                },
                                "required": ["old_text", "new_text"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["path", "hunks"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["patches"],
        "additionalProperties": False,
    },
}


_shell_processes: set[subprocess.Popen] = set()
_shell_process_lock = threading.RLock()


def _stop_process_group(process: subprocess.Popen):
    """Stop processes that remain in the command's original process group.

    Cross-platform: ``os.killpg`` and ``signal.SIGKILL`` only exist on POSIX,
    so on Windows we fall back to terminating the process directly.
    """
    if hasattr(os, "killpg"):
        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGKILL", None)):
            if sig is None:
                continue
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                return
            except OSError:
                return
            time.sleep(0.05)
    else:
        try:
            process.terminate()
        except OSError:
            pass
        time.sleep(0.05)
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass


def _stop_all_shell_processes():
    with _shell_process_lock:
        processes = list(_shell_processes)
    for process in processes:
        _stop_process_group(process)


def _handle_termination_signal(signum, _frame):
    _stop_all_shell_processes()
    raise SystemExit(128 + signum)


atexit.register(_stop_all_shell_processes)
signal.signal(signal.SIGTERM, _handle_termination_signal)


def _run_bash_process(command: str, cwd: Path | None = None) -> tuple[str, int | None]:
    process = None
    try:
        process = subprocess.Popen(
            command, shell=True, cwd=cwd or WORKDIR,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", start_new_session=True,
        )
        with _shell_process_lock:
            _shell_processes.add(process)
        stdout, stderr = process.communicate(timeout=120)
        out = (stdout + stderr).strip()
        return (out[:50000] if out else "(no output)"), process.returncode
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)", None
    except OSError as exc:
        return f"Error: {type(exc).__name__}: {exc}", None
    finally:
        if process is not None:
            _stop_process_group(process)
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            with _shell_process_lock:
                _shell_processes.discard(process)


def _format_bash_result(output: str, exit_code: int | None) -> str:
    if exit_code == 0:
        return output
    if exit_code is None:
        return output
    return f"Error: command exited with status {exit_code}\n{output}"


def run_bash(command: str, cwd: Path | None = None,
             run_in_background: bool = False) -> str:
    # run_in_background is consumed by the dispatcher; direct execution ignores it.
    return _format_bash_result(*_run_bash_process(command, cwd))


def run_read(path: str, limit: int | None = None,
             offset: int = 0, cwd: Path | None = None) -> str:
    try:
        file_path = safe_path(path, cwd)
        lines = _read_text_file(file_path).splitlines()
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
        lines = lines[offset:]
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path | None = None) -> str:
    try:
        fp = safe_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str,
             cwd: Path | None = None) -> str:
    try:
        fp = safe_path(path, cwd)
        text = _read_text_file(fp)
        if old_text not in text:
            return f"Error: text not found in {path}"
        fp.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str, cwd: Path | None = None) -> str:
    import glob as g
    try:
        base = (cwd or WORKDIR).resolve()
        results = []
        for match in g.glob(pattern, root_dir=base):
            if (base / match).resolve().is_relative_to(base):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def _agent_cwd() -> tuple[Path | None, str | None]:
    # Web conversations select a context-local workspace; CLI/task worktrees
    # continue to use the durable assignment registry as a fallback.
    active = current_workdir()
    if active != WORKDIR:
        return active, None
    try:
        return assignment_cwd("agent"), None
    except (FileNotFoundError, ValueError) as exc:
        return None, f"Error: Invalid task assignment: {exc}"


def run_agent_bash(command: str, run_in_background: bool = False) -> str:
    cwd, error = _agent_cwd()
    return error or run_bash(command, cwd, run_in_background)


def run_agent_read(path: str, limit: int | None = None,
                   offset: int = 0) -> str:
    cwd, error = _agent_cwd()
    return error or run_read(path, limit, offset, cwd)


def run_agent_write(path: str, content: str) -> str:
    cwd, error = _agent_cwd()
    return error or run_write(path, content, cwd)


def run_agent_edit(path: str, old_text: str, new_text: str) -> str:
    cwd, error = _agent_cwd()
    return error or run_edit(path, old_text, new_text, cwd)


def run_agent_glob(pattern: str) -> str:
    cwd, error = _agent_cwd()
    return error or run_glob(pattern, cwd)


def run_agent_search_text(query: str, glob: str | None = None,
                          case_sensitive: bool = False,
                          max_results: int = 100) -> str:
    cwd, error = _agent_cwd()
    return error or run_search_text(
        query, glob, case_sensitive, max_results, cwd
    )


def run_agent_apply_patch(patches: list[dict]) -> str:
    cwd, error = _agent_cwd()
    return error or run_apply_patch(patches, cwd)


def call_tool_handler(handler, args: dict, name: str) -> str:
    if not handler:
        return f"Unknown tool: {name}"
    try:
        return str(handler(**(args or {})))
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in todo or "status" not in todo:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
    return todos, None

def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    print(f"  \033[33m[todo] updated {len(CURRENT_TODOS)} item(s)\033[0m")
    return f"Updated {len(CURRENT_TODOS)} todos"


# -- MessageBus and Team Protocols --

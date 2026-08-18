"""Optional adapter for the memory runtime introduced in s09."""

import importlib.util
import os
from pathlib import Path

from .config import MODEL, WORKDIR, client


class NullMemoryRuntime:
    """Keep the integrated runtime usable when s09 is not installed nearby."""

    @staticmethod
    def read_memory_index() -> str:
        return ""

    @staticmethod
    def load_memories(_messages: list) -> str:
        return ""

    @staticmethod
    def extract_memories(_messages: list) -> bool:
        return False

    @staticmethod
    def consolidate_memories() -> None:
        return None


def _memory_runtime_path() -> Path | None:
    configured = os.getenv("MEMORY_RUNTIME_PATH")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(__file__).resolve().parents[2] / "s09_memory" / "code.py",
        WORKDIR.parent / "s09_memory" / "code.py",
        WORKDIR / "s09_memory" / "code.py",
    ]
    return next((path.resolve() for path in candidates if path and path.is_file()), None)


def load_memory_runtime():
    path = _memory_runtime_path()
    if path is None:
        return NullMemoryRuntime()
    spec = importlib.util.spec_from_file_location(
        f"integrated_memory_{id(client)}", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load memory runtime from {path}")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    runtime.WORKDIR = WORKDIR
    runtime.MEMORY_DIR = WORKDIR / ".memory"
    runtime.MEMORY_INDEX = runtime.MEMORY_DIR / "MEMORY.md"
    runtime.client = client
    runtime.MODEL = MODEL
    return runtime


MEMORY_RUNTIME = load_memory_runtime()

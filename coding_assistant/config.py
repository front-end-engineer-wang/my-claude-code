"""Process configuration and terminal I/O."""

import os
import threading
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

_PERMISSION_MODE_ALIASES = {
    "1": "request",
    "ask": "request",
    "request": "request",
    "request_approval": "request",
    "2": "full",
    "allow": "full",
    "full": "full",
    "full_approval": "full",
}
_permission_mode_value = os.getenv("PERMISSION_MODE", "request").strip().lower()
if _permission_mode_value not in _PERMISSION_MODE_ALIASES:
    allowed = "request or full"
    raise ValueError(
        f"Invalid PERMISSION_MODE={_permission_mode_value!r}; expected {allowed}"
    )
PERMISSION_MODE = _PERMISSION_MODE_ALIASES[_permission_mode_value]

SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
PROMPT = "\033[36ms15 >> \033[0m"
CLI_ACTIVE = False


class ConsoleBroker:
    """Serialize normal prompts and worker permission questions on one stdin."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reader = None

    def ask(self, prompt: str) -> str:
        with self._lock:
            return (self.reader or input)(prompt)


CONSOLE = ConsoleBroker()


def terminal_print(text: str):
    if threading.current_thread() is threading.main_thread() or not CLI_ACTIVE:
        print(text)
        return
    line = ""
    if READLINE_AVAILABLE:
        try:
            line = readline.get_line_buffer()
        except Exception:
            line = ""
    print(f"\r\033[K{text}")
    print(PROMPT + line, end="", flush=True)

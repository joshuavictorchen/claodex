"""Constants used across claodex modules."""

from pathlib import Path

AGENTS = ("claude", "codex")
SESSION_NAME = "claodex"
STATE_DIR = Path(".claodex")
PARTICIPANTS_DIR = STATE_DIR / "participants"
CURSORS_DIR = STATE_DIR / "cursors"
DELIVERY_DIR = STATE_DIR / "delivery"
EXCHANGES_DIR = STATE_DIR / "exchanges"
INBOX_DIR = STATE_DIR / "inbox"
UI_DIR = STATE_DIR / "ui"
UI_EVENTS_FILE = UI_DIR / "events.jsonl"
UI_METRICS_FILE = UI_DIR / "metrics.json"

READ_CURSOR_FILES = {
    "claude": CURSORS_DIR / "read-claude.cursor",
    "codex": CURSORS_DIR / "read-codex.cursor",
}
DELIVERY_CURSOR_FILES = {
    "claude": DELIVERY_DIR / "to-claude.cursor",
    "codex": DELIVERY_DIR / "to-codex.cursor",
}

DEFAULT_POLL_SECONDS = 0.5
DEFAULT_COLLAB_TURNS = 500
CONVERGE_SIGNAL = "[CONVERGED]"
COLLAB_SIGNAL = "[COLLAB]"
DEFAULT_TURN_TIMEOUT_SECONDS = 18000

STUCK_SKIP_ATTEMPTS = 3
STUCK_SKIP_SECONDS = 10.0

# tmux layout split percentages
LAYOUT_BOTTOM_PERCENT = 33
LAYOUT_SIDEBAR_PERCENT = 43

# path pattern for claude debug logs (Stop event fallback)
CLAUDE_DEBUG_LOG_PATTERN = "~/.claude/debug/{session_id}.txt"

# regex for the Stop hook dispatch line in claude debug logs.
# anchored on ISO timestamp prefix to filter echoes in tool output.
CLAUDE_STOP_EVENT_RE = (
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)"
    r"\s+\[DEBUG\]\s+Getting matching hook commands for Stop"
)

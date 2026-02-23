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

READ_CURSOR_FILES = {
    "claude": CURSORS_DIR / "read-claude.cursor",
    "codex": CURSORS_DIR / "read-codex.cursor",
}
DELIVERY_CURSOR_FILES = {
    "claude": DELIVERY_DIR / "to-claude.cursor",
    "codex": DELIVERY_DIR / "to-codex.cursor",
}

DEFAULT_POLL_SECONDS = 0.5
DEFAULT_COLLAB_TURNS = 10
DEFAULT_TURN_TIMEOUT_SECONDS = 300

STUCK_SKIP_ATTEMPTS = 3
STUCK_SKIP_SECONDS = 10.0

# seconds of no JSONL growth before declaring a claude turn complete via
# quiescence fallback (used when turn_duration marker is absent)
DEFAULT_CLAUDE_QUIESCENCE_SECONDS = 3.0

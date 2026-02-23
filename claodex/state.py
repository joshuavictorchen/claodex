"""Filesystem state helpers for claodex."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .constants import (
    AGENTS,
    CURSORS_DIR,
    DELIVERY_CURSOR_FILES,
    DELIVERY_DIR,
    EXCHANGES_DIR,
    INBOX_DIR,
    PARTICIPANTS_DIR,
    READ_CURSOR_FILES,
    STATE_DIR,
)
from .errors import ClaodexError


@dataclass(frozen=True)
class Participant:
    """Participant metadata registered by each agent skill.

    Attributes:
        agent: Agent identity, either `claude` or `codex`.
        session_file: Absolute path to the native JSONL session log.
        session_id: Source-native session identifier.
        tmux_pane: tmux pane id (e.g. `%3`).
        cwd: Absolute workspace path from the agent process.
        registered_at: ISO 8601 timestamp with timezone offset.
    """

    agent: str
    session_file: Path
    session_id: str
    tmux_pane: str
    cwd: Path
    registered_at: str


@dataclass(frozen=True)
class SessionParticipants:
    """Both participants for a claodex session."""

    claude: Participant
    codex: Participant

    def for_agent(self, agent: str) -> Participant:
        """Return participant by agent id.

        Args:
            agent: `claude` or `codex`.

        Returns:
            Participant metadata for that agent.
        """
        if agent == "claude":
            return self.claude
        if agent == "codex":
            return self.codex
        raise ClaodexError(f"validation error: unsupported agent: {agent}")


def peer_agent(agent: str) -> str:
    """Return the opposite peer agent."""
    if agent == "claude":
        return "codex"
    if agent == "codex":
        return "claude"
    raise ClaodexError(f"validation error: unsupported agent: {agent}")


def state_root(workspace_root: Path) -> Path:
    """Return absolute claodex state root for a workspace."""
    return workspace_root / STATE_DIR


def participants_dir(workspace_root: Path) -> Path:
    """Return participant directory."""
    return workspace_root / PARTICIPANTS_DIR


def participant_file(workspace_root: Path, agent: str) -> Path:
    """Return participant metadata file path."""
    return participants_dir(workspace_root) / f"{agent}.json"


def read_cursor_file(workspace_root: Path, agent: str) -> Path:
    """Return read cursor file for one agent source."""
    return workspace_root / READ_CURSOR_FILES[agent]


def delivery_cursor_file(workspace_root: Path, target_agent: str) -> Path:
    """Return delivery cursor file for one target agent."""
    return workspace_root / DELIVERY_CURSOR_FILES[target_agent]


def exchanges_dir(workspace_root: Path) -> Path:
    """Return exchange log directory."""
    return workspace_root / EXCHANGES_DIR


def inbox_dir(workspace_root: Path) -> Path:
    """Return inbox fallback directory."""
    return workspace_root / INBOX_DIR


def ensure_state_layout(workspace_root: Path) -> None:
    """Ensure all runtime state directories exist.

    Args:
        workspace_root: Workspace root path.
    """
    for relative_dir in (
        STATE_DIR,
        PARTICIPANTS_DIR,
        CURSORS_DIR,
        DELIVERY_DIR,
        EXCHANGES_DIR,
        INBOX_DIR,
    ):
        (workspace_root / relative_dir).mkdir(parents=True, exist_ok=True)


def ensure_gitignore_entry(workspace_root: Path) -> None:
    """Ensure `.claodex/` is ignored by git.

    Args:
        workspace_root: Workspace root path.
    """
    entry = ".claodex/"
    gitignore = workspace_root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(f"{entry}\n", encoding="utf-8")
        return

    lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    if entry in lines:
        return

    with gitignore.open("a", encoding="utf-8") as handle:
        if lines and lines[-1] != "":
            handle.write("\n")
        handle.write(f"{entry}\n")


def read_json(path: Path) -> dict:
    """Read one JSON object from disk.

    Args:
        path: JSON file path.

    Returns:
        Parsed dictionary payload.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ClaodexError(f"validation error: malformed json: {path}") from exc
    if not isinstance(payload, dict):
        raise ClaodexError(f"validation error: malformed json object: {path}")
    return payload


def write_json(path: Path, payload: dict) -> None:
    """Write one JSON object to disk.

    Args:
        path: Destination file path.
        payload: JSON payload.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _validate_timestamp_with_timezone(value: str, source_path: Path) -> None:
    """Validate strict ISO8601 timestamp with offset.

    Args:
        value: Timestamp to validate.
        source_path: Participant source path for errors.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ClaodexError(
            f"validation error: participant registered_at invalid in {source_path}"
        ) from exc
    if parsed.tzinfo is None:
        raise ClaodexError(
            f"validation error: participant registered_at missing timezone in {source_path}"
        )


def validate_participant_payload(payload: dict, expected_agent: str, source_path: Path) -> None:
    """Validate participant schema.

    Args:
        payload: Raw participant payload.
        expected_agent: Expected agent id for this file.
        source_path: Origin file for diagnostics.
    """
    agent = payload.get("agent")
    if agent != expected_agent:
        raise ClaodexError(f"validation error: participant agent mismatch in {source_path}")

    session_file = payload.get("session_file")
    if not isinstance(session_file, str) or not session_file:
        raise ClaodexError(
            f"validation error: participant session_file missing in {source_path}"
        )
    if not Path(session_file).is_absolute():
        raise ClaodexError(
            f"validation error: participant session_file must be absolute in {source_path}"
        )

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ClaodexError(
            f"validation error: participant session_id missing in {source_path}"
        )

    tmux_pane = payload.get("tmux_pane")
    if not isinstance(tmux_pane, str) or not tmux_pane:
        raise ClaodexError(
            f"validation error: participant tmux_pane missing in {source_path}"
        )

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise ClaodexError(f"validation error: participant cwd missing in {source_path}")
    if not Path(cwd).is_absolute():
        raise ClaodexError(
            f"validation error: participant cwd must be absolute in {source_path}"
        )

    registered_at = payload.get("registered_at")
    if not isinstance(registered_at, str):
        raise ClaodexError(
            f"validation error: participant registered_at invalid in {source_path}"
        )
    _validate_timestamp_with_timezone(registered_at, source_path)


def load_participant(workspace_root: Path, agent: str) -> Participant:
    """Load one participant file.

    Args:
        workspace_root: Workspace root path.
        agent: Agent id to load.

    Returns:
        Parsed participant metadata.
    """
    path = participant_file(workspace_root, agent)
    if not path.exists():
        raise ClaodexError(f"participant missing: {path}")

    payload = read_json(path)
    validate_participant_payload(payload, expected_agent=agent, source_path=path)

    session_file = Path(payload["session_file"]).resolve()
    if not session_file.exists():
        raise ClaodexError(f"participant session file missing: {session_file}")

    return Participant(
        agent=agent,
        session_file=session_file,
        session_id=payload["session_id"],
        tmux_pane=payload["tmux_pane"],
        cwd=Path(payload["cwd"]).resolve(),
        registered_at=payload["registered_at"],
    )


def load_participants(workspace_root: Path) -> SessionParticipants:
    """Load both participants from disk.

    Args:
        workspace_root: Workspace root path.

    Returns:
        Both participant descriptors.
    """
    return SessionParticipants(
        claude=load_participant(workspace_root, "claude"),
        codex=load_participant(workspace_root, "codex"),
    )


def read_cursor(path: Path) -> int:
    """Read strict cursor format, creating `0\n` when missing.

    Args:
        path: Cursor file path.

    Returns:
        Non-negative cursor value.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("0\n", encoding="utf-8")
        return 0

    content = path.read_text(encoding="utf-8", errors="replace")
    if not content.endswith("\n"):
        raise ClaodexError(f"corrupt cursor: {path}")

    value = content.strip()
    if not value.isdigit():
        raise ClaodexError(f"corrupt cursor: {path}")
    return int(value)


def write_cursor(path: Path, value: int) -> None:
    """Write cursor value.

    Args:
        path: Cursor file path.
        value: Non-negative 1-indexed line cursor.
    """
    if value < 0:
        raise ClaodexError("validation error: cursor must be non-negative")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")


def read_read_cursor(workspace_root: Path, source_agent: str) -> int:
    """Read source read cursor.

    Args:
        workspace_root: Workspace root path.
        source_agent: Source agent for the read cursor.

    Returns:
        Cursor value.
    """
    return read_cursor(read_cursor_file(workspace_root, source_agent))


def write_read_cursor(workspace_root: Path, source_agent: str, value: int) -> None:
    """Write source read cursor."""
    write_cursor(read_cursor_file(workspace_root, source_agent), value)


def read_delivery_cursor(workspace_root: Path, target_agent: str) -> int:
    """Read delivery cursor for target agent."""
    return read_cursor(delivery_cursor_file(workspace_root, target_agent))


def write_delivery_cursor(workspace_root: Path, target_agent: str, value: int) -> None:
    """Write delivery cursor for target agent."""
    write_cursor(delivery_cursor_file(workspace_root, target_agent), value)


def count_lines(path: Path) -> int:
    """Count physical file lines.

    Args:
        path: Text file path.

    Returns:
        Number of lines in file.
    """
    if not path.exists():
        return 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        return sum(1 for _ in handle)


def read_lines_between(path: Path, start_line: int, end_line: int | None = None) -> list[str]:
    """Read lines strictly after `start_line` and optionally up to `end_line`.

    Args:
        path: Source text file.
        start_line: Starting cursor (exclusive).
        end_line: Optional ending cursor (inclusive).

    Returns:
        Raw file lines.
    """
    lines: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if line_number <= start_line:
                continue
            if end_line is not None and line_number > end_line:
                break
            lines.append(raw_line)
    return lines


def initialize_cursors_from_line_counts(
    workspace_root: Path,
    participants: SessionParticipants,
) -> None:
    """Initialize all four cursors to current file line counts.

    Args:
        workspace_root: Workspace root path.
        participants: Active participants.
    """
    claude_lines = count_lines(participants.claude.session_file)
    codex_lines = count_lines(participants.codex.session_file)

    write_read_cursor(workspace_root, "claude", claude_lines)
    write_read_cursor(workspace_root, "codex", codex_lines)
    write_delivery_cursor(workspace_root, "claude", codex_lines)
    write_delivery_cursor(workspace_root, "codex", claude_lines)


def cursor_snapshot(workspace_root: Path) -> dict[str, int]:
    """Return read/delivery cursor snapshot for status output.

    Args:
        workspace_root: Workspace root path.

    Returns:
        Mapping of cursor names to values.
    """
    snapshot = {}
    for agent in AGENTS:
        snapshot[f"read-{agent}"] = read_read_cursor(workspace_root, agent)
    for target in AGENTS:
        snapshot[f"to-{target}"] = read_delivery_cursor(workspace_root, target)
    return snapshot

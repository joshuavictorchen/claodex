#!/usr/bin/env python3
"""Native session discovery and multi-turn room-event extraction for group-chat."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

SUPPORTED_SOURCES = ("claude", "codex")
UTC_RFC3339_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
CLAUDE_COMMAND_TAG_PATTERN = re.compile(
    r"<(?P<tag>command-message|command-name|command-args)>(?P<body>.*?)</(?P=tag)>",
    re.DOTALL,
)
GROUP_CHAT_USER_PREFIXES = (
    "/group-chat",
    "$group-chat",
    "/group",
    "$group",
)


class ExtractionError(RuntimeError):
    """Raised when extraction cannot produce a valid payload."""


def resolve_workspace_root(path: Path) -> Path:
    """Resolve workspace root from any path.

    Args:
        path: Candidate path inside or at the workspace.

    Returns:
        Git root path when available, otherwise resolved input path.
    """
    resolved_path = path.resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=resolved_path,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return resolved_path


def encode_claude_project_dir(workspace_root: str) -> str:
    """Encode workspace path using Claude project-directory conventions.

    Args:
        workspace_root: Absolute workspace path.

    Returns:
        Encoded directory string (e.g. ``/a/b`` -> ``-a-b``).
    """
    return workspace_root.replace("/", "-")


def discover_claude_session(workspace_root: Path) -> Path | None:
    """Find the most recent Claude session for a workspace.

    Args:
        workspace_root: Workspace root path.

    Returns:
        Most recently modified Claude JSONL file or None.
    """
    project_dir = (
        Path.home()
        / ".claude"
        / "projects"
        / encode_claude_project_dir(str(workspace_root))
    )
    if not project_dir.is_dir():
        return None

    candidates: list[tuple[Path, float]] = []
    for session_file in project_dir.glob("*.jsonl"):
        try:
            candidates.append((session_file, session_file.stat().st_mtime))
        except OSError:
            continue
    if not candidates:
        return None

    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][0]


def discover_codex_session(
    workspace_root: Path,
    thread_id: str | None = None,
) -> Path | None:
    """Find the most relevant Codex session for a workspace.

    Selection order:
    1. if `thread_id` is provided and appears in a filename, use that file
    2. if `thread_id` is provided and matches `session_meta.payload.id`, use it
    3. otherwise choose most recent file whose `session_meta.payload.cwd` matches

    Args:
        workspace_root: Workspace root path.
        thread_id: Optional Codex thread id.

    Returns:
        Matching Codex JSONL file or None.
    """
    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.is_dir():
        return None

    candidates: list[tuple[Path, float]] = []
    for session_file in sessions_root.rglob("*.jsonl"):
        try:
            candidates.append((session_file, session_file.stat().st_mtime))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[1], reverse=True)

    if thread_id:
        for session_file, _ in candidates:
            if thread_id in session_file.name:
                return session_file

        for session_file, _ in candidates:
            session_meta = _read_session_meta(session_file)
            if not session_meta:
                continue
            payload = session_meta.get("payload", {})
            if payload.get("id") == thread_id:
                return session_file

    workspace_root_text = str(workspace_root)
    for session_file, _ in candidates:
        session_meta = _read_session_meta(session_file)
        if not session_meta:
            continue
        payload = session_meta.get("payload", {})
        if payload.get("cwd") == workspace_root_text:
            return session_file

    return None


def discover_session(
    source: str,
    workspace_root: Path,
    session_file: Path | None = None,
    codex_thread_id: str | None = None,
) -> Path:
    """Resolve source session log path.

    Args:
        source: Session source (`claude` or `codex`).
        workspace_root: Workspace root path.
        session_file: Optional explicit session file path.
        codex_thread_id: Optional preferred Codex thread id.

    Returns:
        Existing session file path.

    Raises:
        ExtractionError: If source is unknown or no session is found.
    """
    if source not in SUPPORTED_SOURCES:
        raise ExtractionError(
            f"unsupported source '{source}', expected one of {SUPPORTED_SOURCES}"
        )

    if session_file is not None:
        if not session_file.exists():
            raise ExtractionError(f"session file not found: {session_file}")
        return session_file

    if source == "claude":
        discovered = discover_claude_session(workspace_root)
    else:
        discovered = discover_codex_session(workspace_root, thread_id=codex_thread_id)

    if discovered is None:
        raise ExtractionError(
            f"no {source} session found for workspace: {workspace_root}"
        )
    return discovered


def extract_room_events_from_window(
    source: str,
    delta_lines: list[str],
    agent_participant: str,
    start_line: int = 0,
) -> dict:
    """Extract multi-turn room events from a native-log delta window.

    Args:
        source: Native source type (`claude` or `codex`).
        delta_lines: Raw JSONL lines in source-log order.
        agent_participant: Participant id corresponding to source.
        start_line: Last processed absolute source line before delta.

    Returns:
        Dictionary with keys:
        - `events`: list of room-event payload fragments
        - `last_success_line`: absolute source line position
        - `warnings`: non-fatal parser warnings

    Raises:
        ExtractionError: If source/participant inputs are invalid.
    """
    if source not in SUPPORTED_SOURCES:
        raise ExtractionError(f"unsupported source '{source}'")
    if agent_participant not in SUPPORTED_SOURCES:
        raise ExtractionError(f"unsupported participant '{agent_participant}'")
    if source != agent_participant:
        raise ExtractionError(
            f"source/participant mismatch: source={source} participant={agent_participant}"
        )
    if start_line < 0:
        raise ExtractionError("start_line must be non-negative")

    parsed_rows: list[dict] = []
    for relative_line, raw_line in enumerate(delta_lines, start=1):
        absolute_line = start_line + relative_line
        if not raw_line.strip():
            parsed_rows.append(
                {
                    "relative_line": relative_line,
                    "absolute_line": absolute_line,
                    "entry": None,
                    "error": "empty line",
                }
            )
            continue
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            parsed_rows.append(
                {
                    "relative_line": relative_line,
                    "absolute_line": absolute_line,
                    "entry": None,
                    "error": str(exc),
                }
            )
            continue
        if not isinstance(parsed, dict):
            parsed_rows.append(
                {
                    "relative_line": relative_line,
                    "absolute_line": absolute_line,
                    "entry": None,
                    "error": "expected JSON object",
                }
            )
            continue
        parsed_rows.append(
            {
                "relative_line": relative_line,
                "absolute_line": absolute_line,
                "entry": parsed,
                "error": None,
            }
        )

    last_success_relative_line = 0
    for row in parsed_rows:
        if row["entry"] is not None:
            last_success_relative_line = row["relative_line"]

    warnings: list[str] = []
    valid_entries: list[dict] = []
    for row in parsed_rows:
        relative_line = row["relative_line"]
        absolute_line = row["absolute_line"]
        parsed = row["entry"]
        error = row["error"]

        if parsed is None:
            if relative_line < last_success_relative_line:
                warnings.append(
                    f"warning: malformed native log entry at line {absolute_line}: {error}"
                )
                continue
            warnings.append(
                "warning: malformed native log tail entry at line "
                f"{absolute_line}: {error}"
            )
            break
        if relative_line > last_success_relative_line:
            break
        valid_entries.append(parsed)

    if source == "claude":
        events = _extract_claude_room_events(valid_entries)
    else:
        events, codex_warnings = _extract_codex_room_events(valid_entries)
        warnings.extend(codex_warnings)

    return {
        "events": events,
        "last_success_line": start_line + last_success_relative_line,
        "warnings": warnings,
    }


def _extract_claude_room_events(entries: list[dict]) -> list[dict]:
    """Extract room events from parsed Claude JSONL entries.

    Args:
        entries: Valid JSONL entries in source order.

    Returns:
        Room-event payload fragments in source order.
    """
    events: list[dict] = []
    pending_assistant_event: dict | None = None

    def flush_pending_assistant() -> None:
        nonlocal pending_assistant_event
        if pending_assistant_event is not None:
            events.append(pending_assistant_event)
            pending_assistant_event = None

    for entry in entries:
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue

        entry_type = entry.get("type")
        message = entry.get("message", {})
        role = message.get("role")
        timestamp = _extract_entry_timestamp(entry)

        if entry_type == "user" and role == "user":
            if _is_tool_result_only_claude_user_entry(message):
                continue
            # user entries define assistant-turn boundaries even when text is empty
            flush_pending_assistant()
            text = _normalize_claude_user_text(
                _extract_claude_user_text(message.get("content"))
            )
            if not timestamp or not text.strip():
                continue
            events.append(
                {
                    "ts": timestamp,
                    "from": "user-claude",
                    "body": text,
                }
            )
            continue

        if entry_type != "assistant" or role != "assistant":
            continue

        if not timestamp:
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue

        text_fragments: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text_value = block.get("text")
            if isinstance(text_value, str):
                text_fragments.append(text_value)

        frame_text = "\n".join(text_fragments)
        if frame_text.strip():
            # final non-empty assistant frame in each turn wins
            pending_assistant_event = {
                "ts": timestamp,
                "from": "claude",
                "body": frame_text,
            }

    flush_pending_assistant()

    return events


def _extract_codex_room_events(entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Extract room events from parsed Codex JSONL entries.

    Args:
        entries: Valid JSONL entries in source order.

    Returns:
        Tuple of:
        - room-event payload fragments in source order
        - non-fatal extraction warnings
    """
    events: list[dict] = []
    warnings: list[str] = []
    pending_assistant_event: dict | None = None
    warned_ambiguous_user_payload = False

    def flush_pending_assistant() -> None:
        nonlocal pending_assistant_event
        if pending_assistant_event is not None:
            events.append(pending_assistant_event)
            pending_assistant_event = None

    for entry in entries:
        entry_type = entry.get("type")
        timestamp = _extract_entry_timestamp(entry)

        if entry_type == "event_msg":
            payload = entry.get("payload", {})
            if payload.get("type") != "user_message":
                continue
            if _has_ambiguous_codex_user_payload(payload) and not warned_ambiguous_user_payload:
                warnings.append(
                    "warning: codex user_message payload contains both message and "
                    "content; preferring message"
                )
                warned_ambiguous_user_payload = True
            # user entries define assistant-turn boundaries even when text is empty
            flush_pending_assistant()
            if not timestamp:
                continue
            user_text = _extract_codex_user_message_text(payload)
            if not user_text.strip():
                continue
            events.append(
                {
                    "ts": timestamp,
                    "from": "user-codex",
                    "body": user_text,
                }
            )
            continue

        if entry_type != "response_item":
            continue

        payload = entry.get("payload", {})
        payload_type = payload.get("type")

        if payload_type != "message" or payload.get("role") != "assistant":
            continue

        assistant_text = _extract_codex_message_text(payload)
        if not timestamp:
            continue
        if assistant_text.strip():
            # final non-empty assistant message in each turn wins
            pending_assistant_event = {
                "ts": timestamp,
                "from": "codex",
                "body": assistant_text,
            }

    flush_pending_assistant()

    return events, warnings


def _has_ambiguous_codex_user_payload(payload: dict) -> bool:
    """Return true when Codex user payload includes both message and content text."""
    if not isinstance(payload.get("message"), str):
        return False

    content = payload.get("content")
    return isinstance(content, str) or isinstance(content, list)


def _extract_claude_user_text(content: object) -> str:
    """Extract user text from Claude message content.

    Args:
        content: Claude message content payload.

    Returns:
        Extracted user text.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_fragments: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text_value = block.get("text")
            if isinstance(text_value, str):
                text_fragments.append(text_value)
        return "\n".join(text_fragments)
    return ""


def _is_tool_result_only_claude_user_entry(message: dict) -> bool:
    """Return true when a Claude user message contains only tool_result blocks.

    Args:
        message: Claude entry `message` object.

    Returns:
        True when the user message is tool plumbing only.
    """
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False

    saw_tool_result = False
    for block in content:
        if not isinstance(block, dict):
            return False
        if block.get("type") != "tool_result":
            return False
        saw_tool_result = True
    return saw_tool_result


def _normalize_claude_user_text(text: str) -> str:
    """Normalize Claude skill-wrapper command tags into plain user text.

    Args:
        text: Raw extracted user text.

    Returns:
        Normalized user-facing message text.
    """
    if not text:
        return ""

    matches = list(CLAUDE_COMMAND_TAG_PATTERN.finditer(text))
    if not matches:
        return _strip_group_chat_prefix(text)

    outside = CLAUDE_COMMAND_TAG_PATTERN.sub("", text)
    if outside.strip():
        return _strip_group_chat_prefix(text)

    by_tag: dict[str, str] = {}
    for match in matches:
        tag_name = match.group("tag")
        body = match.group("body").strip()
        if body:
            by_tag[tag_name] = body

    if "command-args" in by_tag:
        return _strip_group_chat_prefix(by_tag["command-args"])
    if "command-name" in by_tag:
        return _strip_group_chat_prefix(by_tag["command-name"])
    if "command-message" in by_tag:
        return _strip_group_chat_prefix(by_tag["command-message"])
    return _strip_group_chat_prefix(text)


def _extract_codex_user_message_text(payload: dict) -> str:
    """Extract user-message text from a Codex `event_msg` payload.

    Args:
        payload: Codex `event_msg.payload` object.

    Returns:
        User message text.
    """
    message = payload.get("message")
    if isinstance(message, str):
        return _strip_group_chat_prefix(message)

    content = payload.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text_value = block.get("text")
            if isinstance(text_value, str) and text_value.strip():
                parts.append(text_value)
        if parts:
            return _strip_group_chat_prefix("\n".join(parts))

    if isinstance(content, str):
        return _strip_group_chat_prefix(content)
    return ""


def _strip_group_chat_prefix(text: str) -> str:
    """Strip leading group-chat command prefixes from user text.

    Args:
        text: Raw user text.

    Returns:
        Message text without command prefix.
    """
    stripped = text.lstrip()
    for prefix in GROUP_CHAT_USER_PREFIXES:
        if stripped == prefix:
            return ""
        if stripped.startswith(prefix + " "):
            return stripped[len(prefix) :].lstrip()
    return text


def _extract_codex_message_text(payload: dict) -> str:
    """Extract assistant text from a Codex message payload.

    Args:
        payload: Codex `response_item.payload` object.

    Returns:
        Joined assistant text blocks, or empty string if none.
    """
    content = payload.get("content")
    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text_value = block.get("text")
            if isinstance(text_value, str) and text_value.strip():
                text_parts.append(text_value)
        if text_parts:
            return "\n".join(text_parts)

    fallback_text = payload.get("text")
    if isinstance(fallback_text, str):
        return fallback_text
    return ""


def _extract_entry_timestamp(entry: dict) -> str | None:
    """Return a valid UTC RFC3339 timestamp from a JSONL entry.

    Args:
        entry: Parsed JSONL entry.

    Returns:
        Timestamp string, or None when unavailable/invalid.
    """
    timestamp = entry.get("timestamp")
    if not isinstance(timestamp, str):
        return None
    if not UTC_RFC3339_PATTERN.match(timestamp):
        return None
    return timestamp


def _read_session_meta(session_file: Path) -> dict | None:
    """Read the first `session_meta` record from JSONL.

    Args:
        session_file: JSONL session path.

    Returns:
        Session-meta entry or None.
    """
    try:
        with session_file.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(20):
                raw_line = handle.readline()
                if not raw_line:
                    break
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "session_meta":
                    return entry
    except OSError:
        return None
    return None

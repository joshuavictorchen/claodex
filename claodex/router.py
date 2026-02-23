"""Message routing and collab helpers for claodex."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .constants import DEFAULT_CLAUDE_QUIESCENCE_SECONDS, STUCK_SKIP_ATTEMPTS, STUCK_SKIP_SECONDS
from .errors import ClaodexError
from .extract import extract_room_events_from_window
from .state import (
    Participant,
    SessionParticipants,
    count_lines,
    peer_agent,
    read_delivery_cursor,
    read_lines_between,
    read_read_cursor,
    write_delivery_cursor,
    write_read_cursor,
)

HEADER_LINE_PATTERN = re.compile(r"^---\s*(claude|codex|user)\s*---\s*$")


@dataclass
class RoutingConfig:
    """Runtime tuning values for router behavior."""

    poll_seconds: float
    turn_timeout_seconds: int
    claude_quiescence_seconds: float = DEFAULT_CLAUDE_QUIESCENCE_SECONDS


@dataclass
class PendingSend:
    """Metadata for one message sent to an agent."""

    target_agent: str
    before_cursor: int
    sent_text: str


@dataclass
class ResponseTurn:
    """One completed response turn from an agent."""

    agent: str
    text: str
    source_cursor: int


@dataclass
class StuckCursorState:
    """Tracks repeated parse stalls on one read cursor."""

    line: int
    attempts: int
    started_at: float


@dataclass
class TurnEndScan:
    """Result of scanning one JSONL window for a deterministic turn-end marker."""

    marker_line: int | None
    saw_codex_task_started: bool = False


class Router:
    """Coordinates event extraction, delta formatting, and message delivery."""

    def __init__(
        self,
        workspace_root: Path,
        participants: SessionParticipants,
        paste_content: Callable[[str, str], None],
        pane_alive: Callable[[str], bool],
        config: RoutingConfig,
    ) -> None:
        """Initialize router.

        Args:
            workspace_root: Workspace root path.
            participants: Session participants.
            paste_content: Callback for injecting text into a pane.
            pane_alive: Callback returning whether a pane is alive.
            config: Polling and timeout configuration.
        """
        self.workspace_root = workspace_root
        self.participants = participants
        self._paste_content = paste_content
        self._pane_alive = pane_alive
        self.config = config
        self._stuck_state: dict[str, StuckCursorState] = {}

    def refresh_source(self, source_agent: str) -> int:
        """Advance source read cursor by parsing newly appended lines.

        Args:
            source_agent: Source to refresh (`claude` or `codex`).

        Returns:
            Updated read cursor.

        Raises:
            ClaodexError: If cursor integrity checks fail.
        """
        participant = self.participants.for_agent(source_agent)
        cursor = read_read_cursor(self.workspace_root, source_agent)
        line_count = count_lines(participant.session_file)
        if cursor > line_count:
            raise ClaodexError(
                f"read cursor {cursor} exceeds {source_agent} session length {line_count}"
            )

        if cursor == line_count:
            self._stuck_state.pop(source_agent, None)
            return cursor

        delta_lines = read_lines_between(participant.session_file, start_line=cursor, end_line=line_count)
        extraction = extract_room_events_from_window(
            source=source_agent,
            delta_lines=delta_lines,
            agent_participant=source_agent,
            start_line=cursor,
        )
        next_cursor = extraction["last_success_line"]
        if next_cursor < cursor:
            raise ClaodexError("validation error: read cursor cannot move backward")

        if next_cursor == cursor:
            now = time.monotonic()
            previous = self._stuck_state.get(source_agent)
            if previous is None or previous.line != cursor:
                previous = StuckCursorState(line=cursor, attempts=0, started_at=now)
            previous.attempts += 1
            self._stuck_state[source_agent] = previous

            elapsed = now - previous.started_at
            if previous.attempts >= STUCK_SKIP_ATTEMPTS or elapsed >= STUCK_SKIP_SECONDS:
                skipped_cursor = min(cursor + 1, line_count)
                write_read_cursor(self.workspace_root, source_agent, skipped_cursor)
                self._stuck_state.pop(source_agent, None)
                print(
                    "warning: skipped malformed "
                    f"{source_agent} log line at {cursor + 1} after repeated parse failures"
                )
                return skipped_cursor

            return cursor

        write_read_cursor(self.workspace_root, source_agent, next_cursor)
        self._stuck_state.pop(source_agent, None)
        for warning in extraction["warnings"]:
            print(warning)
        return next_cursor

    def _extract_events_between(self, source_agent: str, start_line: int, end_line: int) -> list[dict]:
        """Extract normalized events for a line range.

        Args:
            source_agent: Source session owner.
            start_line: Starting line cursor (exclusive).
            end_line: Ending line cursor (inclusive).

        Returns:
            Extracted room events in source order.
        """
        participant = self.participants.for_agent(source_agent)
        delta_lines = read_lines_between(
            participant.session_file,
            start_line=start_line,
            end_line=end_line,
        )
        extraction = extract_room_events_from_window(
            source=source_agent,
            delta_lines=delta_lines,
            agent_participant=source_agent,
            start_line=start_line,
        )
        events: list[dict] = []
        for event in extraction["events"]:
            sender = event.get("from")
            body = event.get("body")
            if not isinstance(sender, str) or not sender:
                continue
            if not isinstance(body, str) or not body.strip():
                continue

            if sender.startswith("user-"):
                sender = "user"
                body = strip_injected_context(body)
            events.append(
                {
                    "from": sender,
                    "body": body.strip(),
                }
            )
        return events

    def build_delta_for_target(self, target_agent: str) -> tuple[list[dict], int]:
        """Build undelivered peer delta for a target.

        Args:
            target_agent: Message target (`claude` or `codex`).

        Returns:
            Tuple of extracted events and the peer read cursor line.
        """
        peer = peer_agent(target_agent)
        peer_read_cursor = self.refresh_source(peer)
        delivery_cursor = read_delivery_cursor(self.workspace_root, target_agent)
        if delivery_cursor > peer_read_cursor:
            raise ClaodexError(
                f"delivery cursor {delivery_cursor} exceeds peer read cursor {peer_read_cursor} for {target_agent}"
            )
        if delivery_cursor == peer_read_cursor:
            return [], peer_read_cursor

        events = self._extract_events_between(
            source_agent=peer,
            start_line=delivery_cursor,
            end_line=peer_read_cursor,
        )
        return events, peer_read_cursor

    def compose_user_message(self, target_agent: str, user_text: str) -> tuple[str, int | None]:
        """Compose outbound message with any undelivered peer delta.

        Args:
            target_agent: Target agent id.
            user_text: Raw user-entered message.

        Returns:
            Tuple of composed message and optional delivery cursor update.
        """
        user_text = user_text.strip()
        if not user_text:
            raise ClaodexError("validation error: message cannot be empty")

        delta_events, peer_cursor = self.build_delta_for_target(target_agent)
        if not delta_events:
            return user_text, None

        blocks = []
        for event in delta_events:
            blocks.append(render_block(event["from"], event["body"]))
        blocks.append(render_block("user", user_text))
        return "\n\n".join(blocks), peer_cursor

    def send_user_message(self, target_agent: str, user_text: str) -> PendingSend:
        """Send one user message in normal mode.

        Args:
            target_agent: Message target (`claude` or `codex`).
            user_text: User message payload.

        Returns:
            PendingSend metadata for optional response waiting.
        """
        before_cursor = self.refresh_source(target_agent)
        payload, new_delivery_cursor = self.compose_user_message(target_agent, user_text)
        target = self.participants.for_agent(target_agent)
        self._ensure_target_alive(target)
        self._paste_content(target.tmux_pane, payload)
        if new_delivery_cursor is not None:
            write_delivery_cursor(self.workspace_root, target_agent, new_delivery_cursor)
        return PendingSend(
            target_agent=target_agent,
            before_cursor=before_cursor,
            sent_text=payload,
        )

    def send_routed_message(self, target_agent: str, source_agent: str, response_text: str) -> PendingSend:
        """Send one routed collab message from a peer agent.

        Args:
            target_agent: Recipient agent.
            source_agent: Source agent whose response is being routed.
            response_text: Source response text.

        Returns:
            PendingSend metadata for waiting on target response.
        """
        response_text = response_text.strip()
        if not response_text:
            raise ClaodexError("validation error: routed response cannot be empty")

        before_cursor = self.refresh_source(target_agent)
        payload = render_block(source_agent, response_text)

        target = self.participants.for_agent(target_agent)
        self._ensure_target_alive(target)
        self._paste_content(target.tmux_pane, payload)

        source_cursor = read_read_cursor(self.workspace_root, source_agent)
        write_delivery_cursor(self.workspace_root, target_agent, source_cursor)
        return PendingSend(
            target_agent=target_agent,
            before_cursor=before_cursor,
            sent_text=payload,
        )

    def wait_for_response(
        self,
        pending: PendingSend,
        timeout_seconds: float | None = None,
    ) -> ResponseTurn:
        """Wait for the next assistant response from a target agent.

        Args:
            pending: Metadata for the previously sent message.
            timeout_seconds: Optional timeout override.

        Returns:
            Completed response turn.
        """
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.config.turn_timeout_seconds
        )
        deadline = time.monotonic() + timeout

        target_agent = pending.target_agent
        participant = self.participants.for_agent(target_agent)

        observed_cursor = pending.before_cursor
        marker_scan_cursor = pending.before_cursor
        saw_codex_task_started = False
        last_advance_time: float | None = None

        while time.monotonic() < deadline:
            self._ensure_target_alive(participant)
            current_cursor = self.refresh_source(target_agent)
            if current_cursor > observed_cursor:
                last_advance_time = time.monotonic()
            observed_cursor = current_cursor

            if current_cursor > marker_scan_cursor:
                turn_end = self._scan_turn_end_marker(
                    target_agent=target_agent,
                    participant=participant,
                    start_line=marker_scan_cursor,
                    end_line=current_cursor,
                )
                marker_scan_cursor = current_cursor
                saw_codex_task_started = saw_codex_task_started or turn_end.saw_codex_task_started

                if turn_end.marker_line is not None:
                    assistant_text = self._latest_assistant_message_between(
                        source_agent=target_agent,
                        start_line=pending.before_cursor,
                        end_line=turn_end.marker_line,
                    )
                    if assistant_text is None:
                        marker_label = self._turn_end_marker_label(target_agent)
                        raise ClaodexError(
                            "SMOKE SIGNAL: "
                            f"{target_agent} emitted {marker_label} but no assistant message was "
                            "extractable for that turn window; refusing heuristic fallback"
                        )
                    return ResponseTurn(
                        agent=target_agent,
                        text=assistant_text,
                        source_cursor=turn_end.marker_line,
                    )

            # quiescence fallback: claude does not write turn_duration for
            # short text-only turns.  when the JSONL stops growing and the
            # last assistant entry has text (not tool_use), the turn is done.
            if (
                target_agent == "claude"
                and last_advance_time is not None
                and observed_cursor > pending.before_cursor
                and time.monotonic() - last_advance_time
                >= self.config.claude_quiescence_seconds
                and self._is_claude_turn_quiescent(
                    participant, pending.before_cursor, observed_cursor
                )
            ):
                assistant_text = self._latest_assistant_message_between(
                    source_agent=target_agent,
                    start_line=pending.before_cursor,
                    end_line=observed_cursor,
                )
                if assistant_text is not None:
                    return ResponseTurn(
                        agent=target_agent,
                        text=assistant_text,
                        source_cursor=observed_cursor,
                    )

            time.sleep(self.config.poll_seconds)

        marker_label = self._turn_end_marker_label(target_agent)
        saw_assistant_output = False
        if observed_cursor > pending.before_cursor:
            events = self._extract_events_between(
                source_agent=target_agent,
                start_line=pending.before_cursor,
                end_line=observed_cursor,
            )
            saw_assistant_output = any(event["from"] == target_agent for event in events)

        timeout_text = f"{timeout:g}s"
        if target_agent == "codex" and saw_codex_task_started:
            raise ClaodexError(
                "SMOKE SIGNAL: codex emitted task_started but no task_complete marker "
                f"within {timeout_text}; refusing heuristic fallback"
            )
        if saw_assistant_output:
            raise ClaodexError(
                "SMOKE SIGNAL: "
                f"{target_agent} emitted assistant output but no {marker_label} marker arrived "
                f"within {timeout_text}; refusing heuristic fallback"
            )
        raise ClaodexError(
            "SMOKE SIGNAL: "
            f"missing {marker_label} marker from {target_agent} within {timeout_text}"
        )

    def _scan_turn_end_marker(
        self,
        target_agent: str,
        participant: Participant,
        start_line: int,
        end_line: int,
    ) -> TurnEndScan:
        """Scan one line window for the target's deterministic turn-end marker.

        Args:
            target_agent: Agent whose turn is being awaited.
            participant: Participant metadata for the target.
            start_line: Starting cursor (exclusive).
            end_line: Ending cursor (inclusive).

        Returns:
            Scan result containing marker line (if found) and codex lifecycle hints.
        """
        if target_agent == "codex":
            return self._scan_codex_turn_end_marker(
                participant=participant,
                start_line=start_line,
                end_line=end_line,
            )
        if target_agent == "claude":
            return self._scan_claude_turn_end_marker(
                participant=participant,
                start_line=start_line,
                end_line=end_line,
            )
        raise ClaodexError(f"validation error: unsupported target agent: {target_agent}")

    def _scan_codex_turn_end_marker(
        self,
        participant: Participant,
        start_line: int,
        end_line: int,
    ) -> TurnEndScan:
        """Scan a Codex line window for task lifecycle markers.

        Args:
            participant: Codex participant metadata.
            start_line: Starting cursor (exclusive).
            end_line: Ending cursor (inclusive).

        Returns:
            Scan result. Marker line is the first `task_complete` in the window.
        """
        saw_started = False
        first_complete_without_started: int | None = None
        lines = read_lines_between(participant.session_file, start_line=start_line, end_line=end_line)
        for offset, raw_line in enumerate(lines, start=1):
            absolute_line = start_line + offset
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "event_msg":
                continue

            payload = entry.get("payload", {})
            if not isinstance(payload, dict):
                continue

            marker = payload.get("type")
            if marker == "task_started":
                saw_started = True
            elif marker == "task_complete":
                if saw_started:
                    return TurnEndScan(marker_line=absolute_line, saw_codex_task_started=True)
                if first_complete_without_started is None:
                    first_complete_without_started = absolute_line

        if saw_started:
            # when a new task_started is observed in this window, require a
            # task_complete that appears after it. this prevents latching onto
            # stale task_complete markers that may precede the new turn.
            return TurnEndScan(marker_line=None, saw_codex_task_started=True)
        return TurnEndScan(
            marker_line=first_complete_without_started,
            saw_codex_task_started=False,
        )

    def _scan_claude_turn_end_marker(
        self,
        participant: Participant,
        start_line: int,
        end_line: int,
    ) -> TurnEndScan:
        """Scan a Claude line window for `system.turn_duration`.

        Args:
            participant: Claude participant metadata.
            start_line: Starting cursor (exclusive).
            end_line: Ending cursor (inclusive).

        Returns:
            Scan result. Marker line is the first `turn_duration` in the window.
        """
        lines = read_lines_between(participant.session_file, start_line=start_line, end_line=end_line)
        for offset, raw_line in enumerate(lines, start=1):
            absolute_line = start_line + offset
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "system":
                continue
            if entry.get("subtype") != "turn_duration":
                continue
            return TurnEndScan(marker_line=absolute_line)
        return TurnEndScan(marker_line=None)

    def _is_claude_turn_quiescent(
        self,
        participant: Participant,
        start_line: int,
        end_line: int,
    ) -> bool:
        """Check whether a Claude JSONL window ends in a turn-complete state.

        Scans backwards from end_line for the last assistant or user entry.
        Returns True only when the last such entry is an assistant message
        whose content does not include tool_use blocks (indicating the agent
        is finished rather than mid-tool-chain).

        Args:
            participant: Claude participant metadata.
            start_line: Window start cursor (exclusive).
            end_line: Window end cursor (inclusive).

        Returns:
            True when the window tail looks like a completed turn.
        """
        lines = read_lines_between(
            participant.session_file, start_line=start_line, end_line=end_line
        )
        for raw_line in reversed(lines):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("isSidechain") or entry.get("isMeta"):
                continue

            entry_type = entry.get("type")

            # skip non-message entries (progress, file-history-snapshot, etc.)
            if entry_type not in ("assistant", "user"):
                continue

            message = entry.get("message", {})
            if not isinstance(message, dict):
                continue

            role = message.get("role")
            content = message.get("content")

            if entry_type == "assistant" and role == "assistant":
                # tool_use in content means claude is mid-tool-chain
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            return False
                return True

            if entry_type == "user" and role == "user":
                # tool_result-only user entry means waiting for next assistant
                if isinstance(content, list) and content:
                    all_tool_result = all(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content
                    )
                    if all_tool_result:
                        return False
                return False

        return False

    def _latest_assistant_message_between(
        self,
        source_agent: str,
        start_line: int,
        end_line: int,
    ) -> str | None:
        """Return the latest assistant message body from one source line window."""
        events = self._extract_events_between(
            source_agent=source_agent,
            start_line=start_line,
            end_line=end_line,
        )
        assistant_events = [event for event in events if event["from"] == source_agent]
        if not assistant_events:
            return None
        return assistant_events[-1]["body"]

    def _turn_end_marker_label(self, target_agent: str) -> str:
        """Return human-readable marker contract for one agent."""
        if target_agent == "codex":
            return "event_msg.payload.type=task_complete"
        if target_agent == "claude":
            return "system.subtype=turn_duration"
        raise ClaodexError(f"validation error: unsupported target agent: {target_agent}")

    def _ensure_target_alive(self, participant: Participant) -> None:
        """Fail fast if a target pane is dead.

        Args:
            participant: Participant metadata.
        """
        if not self._pane_alive(participant.tmux_pane):
            raise ClaodexError(f"target pane is not alive: {participant.agent} ({participant.tmux_pane})")


def render_block(source: str, body: str) -> str:
    """Render one outbound protocol block.

    Args:
        source: Block source label (`claude`, `codex`, or `user`).
        body: Block text body.

    Returns:
        Formatted multi-line block.
    """
    body = body.strip()
    if not body:
        raise ClaodexError("validation error: block body cannot be empty")
    return f"--- {source} ---\n{body}"


def strip_injected_context(message: str) -> str:
    """Strip nested claodex header blocks from forwarded user messages.

    Args:
        message: Raw extracted user message.

    Returns:
        Most recent `user` block when a message follows claodex block shape,
        otherwise the original message.
    """
    text = message.strip()
    if not text.startswith("---"):
        return message

    lines = text.splitlines()
    blocks: list[tuple[str, list[str]]] = []
    current_source: str | None = None
    current_lines: list[str] = []

    for line in lines:
        header_match = HEADER_LINE_PATTERN.match(line.strip())
        if header_match:
            if current_source is not None:
                blocks.append((current_source, current_lines))
            current_source = header_match.group(1)
            current_lines = []
            continue

        if current_source is None:
            return message
        current_lines.append(line)

    if current_source is None:
        return message
    blocks.append((current_source, current_lines))

    if not blocks:
        return message

    for source, body_lines in reversed(blocks):
        if source != "user":
            continue
        body = "\n".join(body_lines).strip()
        if body:
            return body
    return message


def count_words(text: str) -> int:
    """Return whitespace-delimited word count.

    Args:
        text: Source text.

    Returns:
        Word count.
    """
    return len(text.strip().split())

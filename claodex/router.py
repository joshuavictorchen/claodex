"""Message routing and collab helpers for claodex."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .constants import (
    AGENTS,
    CLAUDE_DEBUG_LOG_PATTERN,
    CLAUDE_STOP_EVENT_RE,
    STUCK_SKIP_ATTEMPTS,
    STUCK_SKIP_SECONDS,
)

# meta user-row patterns that should be ignored during turn anchoring and
# interference detection.  these appear in the JSONL as user entries but do
# not represent real human input.
_META_USER_PATTERNS = (
    "<command-name>",
    "<command-message>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<task-notification>",
    "This session is being continued",
    "<system-reminder>",
)
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


@dataclass
class PendingSend:
    """Metadata for one message sent to an agent."""

    target_agent: str
    before_cursor: int
    sent_text: str
    # structured (source, body) pairs that compose the payload, built at send
    # time so the exchange log never has to reparse sent_text
    blocks: list[tuple[str, str]] = field(default_factory=list)
    sent_at: datetime | None = None


@dataclass
class ResponseTurn:
    """One completed response turn from an agent."""

    agent: str
    text: str
    source_cursor: int
    received_at: datetime | None = None


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
        warning_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Initialize router.

        Args:
            workspace_root: Workspace root path.
            participants: Session participants.
            paste_content: Callback for injecting text into a pane.
            pane_alive: Callback returning whether a pane is alive.
            config: Polling and timeout configuration.
            warning_callback: Optional callback for non-fatal warnings.
        """
        self.workspace_root = workspace_root
        self.participants = participants
        self._paste_content = paste_content
        self._pane_alive = pane_alive
        self.config = config
        self._warning_callback = warning_callback
        self._stuck_state: dict[str, StuckCursorState] = {}
        # byte offset into the claude debug log to avoid re-reading from the start
        self._debug_log_offset: int = 0
        # latched stop-event flags for poll_for_response, keyed by
        # (target_agent, before_cursor) to survive across idle polls
        self._poll_stop_seen: set[tuple[str, int]] = set()
        self._stop_event_re = re.compile(CLAUDE_STOP_EVENT_RE)

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
                self._emit_warning(
                    "warning: skipped malformed "
                    f"{source_agent} log line at {cursor + 1} after repeated parse failures"
                )
                return skipped_cursor

            return cursor

        write_read_cursor(self.workspace_root, source_agent, next_cursor)
        self._stuck_state.pop(source_agent, None)
        for warning in extraction["warnings"]:
            self._emit_warning(warning)
        return next_cursor

    def _emit_warning(self, message: str) -> None:
        """Emit a non-fatal router warning."""
        if self._warning_callback is not None:
            self._warning_callback(message)

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

    def sync_delivery_cursors(self) -> None:
        """Align both delivery cursors with current peer read positions.

        This is used when collaboration terminates after receiving a final
        response but before routing it onward. Advancing delivery cursors at
        shutdown avoids stale collab rows leaking into the next normal-mode
        user send as undelivered delta.
        """
        for target_agent in AGENTS:
            peer = peer_agent(target_agent)
            peer_read_cursor = self.refresh_source(peer)
            write_delivery_cursor(self.workspace_root, target_agent, peer_read_cursor)

    def compose_user_message(
        self, target_agent: str, user_text: str
    ) -> tuple[str, list[tuple[str, str]], int | None]:
        """Compose outbound message with any undelivered peer delta.

        Args:
            target_agent: Target agent id.
            user_text: Raw user-entered message.

        Returns:
            Tuple of (rendered payload, structured (source, body) block list,
            optional delivery cursor update).
        """
        user_text = user_text.strip()
        if not user_text:
            raise ClaodexError("validation error: message cannot be empty")

        delta_events, peer_cursor = self.build_delta_for_target(target_agent)
        structured: list[tuple[str, str]] = [
            (event["from"], event["body"]) for event in delta_events
        ]
        structured.append(("user", user_text))

        rendered = [render_block(src, body) for src, body in structured]
        return "\n\n".join(rendered), structured, peer_cursor

    def send_user_message(self, target_agent: str, user_text: str) -> PendingSend:
        """Send one user message in normal mode.

        Args:
            target_agent: Message target (`claude` or `codex`).
            user_text: User message payload.

        Returns:
            PendingSend metadata for optional response waiting.
        """
        before_cursor = self.refresh_source(target_agent)
        payload, blocks, new_delivery_cursor = self.compose_user_message(
            target_agent, user_text
        )
        target = self.participants.for_agent(target_agent)
        self._ensure_target_alive(target)
        sent_at = datetime.now(timezone.utc)
        self._paste_content(target.tmux_pane, payload)
        if new_delivery_cursor is not None:
            write_delivery_cursor(self.workspace_root, target_agent, new_delivery_cursor)
        return PendingSend(
            target_agent=target_agent,
            before_cursor=before_cursor,
            sent_text=payload,
            blocks=blocks,
            sent_at=sent_at,
        )

    def send_routed_message(
        self,
        target_agent: str,
        source_agent: str,
        response_text: str,
        user_interjections: list[str] | None = None,
    ) -> PendingSend:
        """Send one routed collab message from a peer agent.

        Args:
            target_agent: Recipient agent.
            source_agent: Source agent whose response is being routed.
            response_text: Source response text.
            user_interjections: Optional user messages to append after
                the routed peer response. Each becomes a separate
                ``--- user ---`` block in the payload.

        Returns:
            PendingSend metadata for waiting on target response.
        """
        response_text = response_text.strip()
        if not response_text:
            raise ClaodexError("validation error: routed response cannot be empty")

        before_cursor = self.refresh_source(target_agent)
        delta_events, peer_cursor = self.build_delta_for_target(target_agent)

        # include undelivered user rows from the source log, but skip source
        # assistant rows because response_text already forwards that response
        blocks: list[tuple[str, str]] = [
            (event["from"], event["body"])
            for event in delta_events
            if event["from"] != source_agent
        ]
        blocks.append((source_agent, response_text))
        for text in user_interjections or ():
            text = text.strip()
            if text:
                blocks.append(("user", text))

        payload = "\n\n".join(render_block(source, body) for source, body in blocks)

        target = self.participants.for_agent(target_agent)
        self._ensure_target_alive(target)
        sent_at = datetime.now(timezone.utc)
        self._paste_content(target.tmux_pane, payload)

        write_delivery_cursor(self.workspace_root, target_agent, peer_cursor)
        return PendingSend(
            target_agent=target_agent,
            before_cursor=before_cursor,
            sent_text=payload,
            blocks=blocks,
            sent_at=sent_at,
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
        # use the actual send timestamp from PendingSend; fall back to now
        # for callers that construct PendingSend manually (e.g. tests)
        send_time = pending.sent_at or datetime.now(timezone.utc)

        target_agent = pending.target_agent
        participant = self.participants.for_agent(target_agent)

        observed_cursor = pending.before_cursor
        marker_scan_cursor = pending.before_cursor
        saw_codex_task_started = False
        saw_stop_event = False

        while time.monotonic() < deadline:
            self._ensure_target_alive(participant)
            current_cursor = self.refresh_source(target_agent)
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
                        received_at=datetime.now(timezone.utc),
                    )

                # interference detection: if a non-meta user row appeared
                # after the anchor, someone typed directly into the agent's
                # terminal during the collab turn.
                if target_agent == "claude":
                    interference = self._detect_interference(
                        participant=participant,
                        before_cursor=pending.before_cursor,
                        current_cursor=current_cursor,
                        sent_text=pending.sent_text,
                    )
                    if interference is not None:
                        raise ClaodexError(
                            f"interference detected in {target_agent} session: "
                            f"unexpected user input while waiting for collab response. "
                            f"snippet: {interference!r}"
                        )

            # stop-event fallback: when turn_duration is absent, check the
            # claude debug log for a Stop hook dispatch event.  the flag is
            # latched so consuming the debug log bytes is not repeated after
            # the event is found — we just wait for assistant text to appear.
            if target_agent == "claude" and current_cursor > pending.before_cursor:
                if not saw_stop_event:
                    saw_stop_event = self._scan_claude_debug_stop_event(
                        participant, send_time
                    )
                if saw_stop_event:
                    assistant_text = self._latest_assistant_message_between(
                        source_agent=target_agent,
                        start_line=pending.before_cursor,
                        end_line=current_cursor,
                    )
                    if assistant_text is not None:
                        return ResponseTurn(
                            agent=target_agent,
                            text=assistant_text,
                            source_cursor=current_cursor,
                            received_at=datetime.now(timezone.utc),
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

    def poll_for_response(self, pending: PendingSend) -> ResponseTurn | None:
        """Non-blocking single-pass check for a completed agent response.

        Same marker detection logic as wait_for_response but performs one
        scan pass and returns immediately.

        Args:
            pending: Metadata for a previously sent message.

        Returns:
            ResponseTurn if the agent has completed its turn, None otherwise.
        """
        target_agent = pending.target_agent
        participant = self.participants.for_agent(target_agent)

        if not self._pane_alive(participant.tmux_pane):
            return None

        current_cursor = self.refresh_source(target_agent)
        if current_cursor <= pending.before_cursor:
            return None

        # check primary turn-end marker (turn_duration / task_complete)
        turn_end = self._scan_turn_end_marker(
            target_agent=target_agent,
            participant=participant,
            start_line=pending.before_cursor,
            end_line=current_cursor,
        )
        if turn_end.marker_line is not None:
            assistant_text = self._latest_assistant_message_between(
                source_agent=target_agent,
                start_line=pending.before_cursor,
                end_line=turn_end.marker_line,
            )
            if assistant_text is not None:
                # clean up any latched stop-event entry for this watch
                self._poll_stop_seen.discard((target_agent, pending.before_cursor))
                return ResponseTurn(
                    agent=target_agent,
                    text=assistant_text,
                    source_cursor=turn_end.marker_line,
                    received_at=datetime.now(timezone.utc),
                )

        # stop-event fallback for claude; latch the flag so consuming
        # debug-log bytes on one poll doesn't lose the event on the next
        if target_agent == "claude":
            send_time = pending.sent_at or datetime.now(timezone.utc)
            latch_key = (target_agent, pending.before_cursor)
            saw_stop_event = latch_key in self._poll_stop_seen
            if not saw_stop_event:
                saw_stop_event = self._scan_claude_debug_stop_event(
                    participant, send_time
                )
                if saw_stop_event:
                    self._poll_stop_seen.add(latch_key)
            if saw_stop_event:
                assistant_text = self._latest_assistant_message_between(
                    source_agent=target_agent,
                    start_line=pending.before_cursor,
                    end_line=current_cursor,
                )
                if assistant_text is not None:
                    self._poll_stop_seen.discard(latch_key)
                    return ResponseTurn(
                        agent=target_agent,
                        text=assistant_text,
                        source_cursor=current_cursor,
                        received_at=datetime.now(timezone.utc),
                    )

        return None

    def clear_poll_latch(self, agent: str, before_cursor: int) -> None:
        """Remove a latched stop-event entry for a discarded watch.

        Args:
            agent: Agent name.
            before_cursor: The before_cursor from the PendingSend.
        """
        self._poll_stop_seen.discard((agent, before_cursor))

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

    def _scan_claude_debug_stop_event(
        self,
        participant: Participant,
        send_time: datetime,
    ) -> bool:
        """Check the claude debug log for a Stop event after send_time.

        Reads from a stored byte offset to avoid re-scanning the whole file.

        Args:
            participant: Claude participant metadata.
            send_time: UTC timestamp of the injected message.

        Returns:
            True when a Stop event with a timestamp after send_time is found.
        """
        # debug logs currently emit millisecond timestamps. floor send_time to
        # the same precision to avoid rejecting same-millisecond Stop events.
        if send_time.tzinfo is None:
            send_time = send_time.replace(tzinfo=timezone.utc)
        else:
            send_time = send_time.astimezone(timezone.utc)
        send_time = send_time.replace(
            microsecond=(send_time.microsecond // 1000) * 1000
        )

        debug_path = Path(
            CLAUDE_DEBUG_LOG_PATTERN.format(session_id=participant.session_id)
        ).expanduser()
        if not debug_path.exists():
            return False

        try:
            file_size = debug_path.stat().st_size
            # reset offset if file was truncated or rotated
            if file_size < self._debug_log_offset:
                self._debug_log_offset = 0
            with debug_path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._debug_log_offset)
                new_content = fh.read()
                self._debug_log_offset = fh.tell()
        except OSError:
            return False

        if not new_content:
            return False

        for line in new_content.splitlines():
            match = self._stop_event_re.match(line)
            if match is None:
                continue
            try:
                event_time = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            except ValueError:
                continue
            if event_time >= send_time:
                return True
        return False

    def _detect_interference(
        self,
        participant: Participant,
        before_cursor: int,
        current_cursor: int,
        sent_text: str,
    ) -> str | None:
        """Detect unexpected user input during a collab wait.

        Scans JSONL between before_cursor and current_cursor for non-meta user
        rows. The anchor is identified by matching against sent_text; any
        additional non-meta user row (including a non-matching first row) is
        interference.

        Args:
            participant: Target participant metadata.
            before_cursor: Cursor before the injected send.
            current_cursor: Current read cursor position.
            sent_text: The text we injected (for anchor matching).

        Returns:
            A snippet of the interfering user text, or None if clean.
        """
        lines = read_lines_between(
            participant.session_file,
            start_line=before_cursor,
            end_line=current_cursor,
        )

        normalized_sent = _normalize_for_anchor(sent_text)
        anchor_found = False
        for raw_line in lines:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "user":
                continue

            message = entry.get("message", {})
            if not isinstance(message, dict):
                continue
            content = message.get("content", "")

            # tool_result entries are part of the agent's tool chain, not user input
            if isinstance(content, list):
                if all(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                ):
                    continue

            # flatten content to text for meta and anchor checks
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            else:
                continue

            if _is_meta_user_text(text):
                continue

            # anchor match: the JSONL may record the full pasted text (with
            # protocol headers) or just the content portion, so check both
            # equality and containment in either direction.
            if not anchor_found:
                normalized_text = _normalize_for_anchor(text)
                if (
                    normalized_text == normalized_sent
                    or normalized_text in normalized_sent
                    or normalized_sent in normalized_text
                ):
                    anchor_found = True
                    continue
                # first non-meta user row doesn't match our send — interference
                snippet = text[:120]
                return snippet

            # any non-meta user row after the anchor is interference
            snippet = text[:120]
            return snippet

        return None

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
            return "system.subtype=turn_duration or debug-log Stop event"
        raise ClaodexError(f"validation error: unsupported target agent: {target_agent}")

    def _ensure_target_alive(self, participant: Participant) -> None:
        """Fail fast if a target pane is dead.

        Args:
            participant: Participant metadata.
        """
        if not self._pane_alive(participant.tmux_pane):
            raise ClaodexError(f"target pane is not alive: {participant.agent} ({participant.tmux_pane})")


def _normalize_for_anchor(text: str) -> str:
    """Normalize text for anchor comparison.

    Collapses whitespace and strips to handle minor formatting differences
    between what we sent and what appears in the JSONL.

    Args:
        text: Raw text to normalize.

    Returns:
        Normalized text for comparison.
    """
    return " ".join(text.split())


def _is_meta_user_text(text: str) -> bool:
    """Return True if user text matches a known meta pattern.

    Meta user rows are injected by the Claude Code runtime (command wrappers,
    task notifications, continuation boilerplate) and should be ignored during
    turn anchoring and interference detection.

    Args:
        text: Flattened user message text.

    Returns:
        True if the text starts with or contains a meta pattern.
    """
    stripped = text.strip()
    for pattern in _META_USER_PATTERNS:
        if stripped.startswith(pattern):
            return True
    return False


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

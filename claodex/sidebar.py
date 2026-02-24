"""Curses sidebar for claodex metrics, logs, and shell input."""

from __future__ import annotations

import curses
import json
import shlex
import subprocess
import sys
import textwrap
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .state import ui_events_file, ui_metrics_file

METRICS_POLL_SECONDS = 0.5
LOOP_TIMEOUT_MILLISECONDS = 100
LOG_BUFFER_MAX = 4000
SHELL_TIMEOUT_SECONDS = 30
SHELL_MAX_LINES = 100
SHELL_MAX_BYTES = 10 * 1024
INTERACTIVE_COMMANDS = frozenset({"vim", "nvim", "nano", "less", "more", "top", "htop"})
LOG_PAGE_SCROLL_LINES = 3
# gap rotates clockwise: top-right → down-right → bottom → up-left → top
SPINNER_FRAMES = ("⣷", "⣯", "⣟", "⡿", "⢿", "⣻", "⣽", "⣾")

PAIR_CODEX = 1
PAIR_CLAUDE = 2
PAIR_ERROR = 3
PAIR_SHELL = 4
PAIR_SYSTEM = 5
PAIR_MODE = 6


@dataclass(frozen=True)
class LogEntry:
    """One sidebar log entry."""

    timestamp: datetime
    kind: str
    message: str
    agent: str | None = None
    target: str | None = None


class SidebarApplication:
    """Owns sidebar rendering loop and event polling."""

    def __init__(self, workspace_root: Path) -> None:
        """Initialize sidebar state.

        Args:
            workspace_root: Workspace root path.
        """
        self._workspace_root = workspace_root
        self._events_path = ui_events_file(workspace_root)
        self._metrics_path = ui_metrics_file(workspace_root)
        self._metrics = _default_metrics_snapshot()
        self._entries: deque[LogEntry] = deque(maxlen=LOG_BUFFER_MAX)
        self._input_buffer: str = ""
        self._event_offset: int = 0
        self._event_fragment: str = ""
        self._last_metrics_poll: float = 0.0
        self._scroll_offset: int = 0
        self._last_log_height: int = 1
        self._colors_enabled = False
        self._spinner_index: int = 0

    def run(self) -> int:
        """Run sidebar app until interrupted."""
        self._clear_terminal_scrollback()
        try:
            curses.wrapper(self._curses_main)
        except KeyboardInterrupt:
            return 0
        return 0

    def _curses_main(self, stdscr: "curses._CursesWindow") -> None:
        """Main curses loop."""
        stdscr.keypad(True)
        stdscr.timeout(LOOP_TIMEOUT_MILLISECONDS)
        curses.noecho()
        curses.cbreak()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self._init_colors()

        while True:
            self._poll_metrics()
            self._poll_events()
            self._render(stdscr)

            try:
                key = stdscr.get_wch()
            except curses.error:
                continue

            if key == curses.KEY_RESIZE:
                continue

            self._handle_input_key(key)

    def _init_colors(self) -> None:
        """Initialize curses color pairs with fallback handling."""
        if not curses.has_colors():
            self._colors_enabled = False
            return

        try:
            curses.start_color()
            curses.use_default_colors()
            codex_color = 116 if curses.COLORS >= 256 else curses.COLOR_CYAN
            claude_color = 216 if curses.COLORS >= 256 else curses.COLOR_YELLOW
            shell_color = 250 if curses.COLORS >= 256 else curses.COLOR_WHITE
            mode_color = curses.COLOR_GREEN
            curses.init_pair(PAIR_CODEX, codex_color, -1)
            curses.init_pair(PAIR_CLAUDE, claude_color, -1)
            curses.init_pair(PAIR_ERROR, curses.COLOR_RED, -1)
            curses.init_pair(PAIR_SHELL, shell_color, -1)
            curses.init_pair(PAIR_SYSTEM, curses.COLOR_WHITE, -1)
            curses.init_pair(PAIR_MODE, mode_color, -1)
            self._colors_enabled = True
        except curses.error:
            self._colors_enabled = False

    def _poll_metrics(self) -> None:
        """Refresh metrics snapshot on the poll interval."""
        now = time.monotonic()
        if now - self._last_metrics_poll < METRICS_POLL_SECONDS:
            return
        self._last_metrics_poll = now
        self._metrics = _load_metrics_snapshot(self._metrics_path, self._metrics)

    def _poll_events(self) -> None:
        """Tail persisted events.jsonl and append new log entries."""
        for raw_line in self._read_event_lines():
            entry = _parse_event_line(raw_line)
            if entry is not None:
                self._entries.append(entry)

    def _read_event_lines(self) -> list[str]:
        """Read newly appended lines from the event file."""
        if not self._events_path.exists():
            return []

        try:
            file_size = self._events_path.stat().st_size
        except OSError:
            return []

        if file_size < self._event_offset:
            self._event_offset = 0
            self._event_fragment = ""

        try:
            with self._events_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._event_offset)
                chunk = handle.read()
                self._event_offset = handle.tell()
        except OSError:
            return []

        if not chunk:
            return []

        data = self._event_fragment + chunk
        lines: list[str] = []
        self._event_fragment = ""

        for part in data.splitlines(keepends=True):
            if part.endswith("\n"):
                lines.append(part.rstrip("\n"))
            else:
                self._event_fragment = part

        return lines

    def _handle_input_key(self, key: str | int) -> None:
        """Update shell input buffer or run command."""
        if key == curses.KEY_PPAGE:
            self._scroll_offset += LOG_PAGE_SCROLL_LINES
            return

        if key == curses.KEY_NPAGE:
            self._scroll_offset = max(0, self._scroll_offset - LOG_PAGE_SCROLL_LINES)
            return

        if key in ("\n", "\r") or key == curses.KEY_ENTER:
            command = self._input_buffer.strip()
            self._input_buffer = ""
            if command:
                self._run_shell_command(command)
            return

        if key in ("\b", "\x7f") or key == curses.KEY_BACKSPACE:
            self._input_buffer = self._input_buffer[:-1]
            return

        if key == "\x03":
            raise KeyboardInterrupt

        if isinstance(key, str) and key.isprintable():
            self._input_buffer += key

    @staticmethod
    def _clear_terminal_scrollback() -> None:
        """Clear terminal display and scrollback before curses takes control."""
        if not sys.stdout.isatty():
            return
        sys.stdout.write("\033[2J\033[H\033[3J")
        sys.stdout.flush()

    def _run_shell_command(self, command: str) -> None:
        """Execute one shell command and append output to local log."""
        self._append_shell_entry(f"$ {command}")

        if _looks_interactive_command(command):
            self._append_shell_entry("[interactive command not supported]")
            return

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=SHELL_TIMEOUT_SECONDS,
                cwd=str(self._workspace_root),
            )
            lines, truncated = _collect_capped_output(
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                max_lines=SHELL_MAX_LINES,
                max_bytes=SHELL_MAX_BYTES,
            )
            for line in lines:
                self._append_shell_entry(line)
            if truncated:
                self._append_shell_entry("[truncated]")
            self._append_shell_entry(f"(exit {result.returncode})")
        except subprocess.TimeoutExpired as exc:
            stdout = _as_text(exc.stdout)
            stderr = _as_text(exc.stderr)
            lines, truncated = _collect_capped_output(
                stdout=stdout,
                stderr=stderr,
                max_lines=SHELL_MAX_LINES,
                max_bytes=SHELL_MAX_BYTES,
            )
            for line in lines:
                self._append_shell_entry(line)
            if truncated:
                self._append_shell_entry("[truncated]")
            self._append_shell_entry("[timeout]")
        except Exception as exc:  # noqa: BLE001
            self._append_shell_entry(f"[error] {exc}")

    def _append_shell_entry(self, message: str) -> None:
        """Append one shell-local log entry."""
        self._entries.append(
            LogEntry(
                timestamp=datetime.now().astimezone(),
                kind="shell",
                message=message,
            )
        )

    def _render(self, stdscr: "curses._CursesWindow") -> None:
        """Render all sidebar sections."""
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height <= 0 or width <= 0:
            stdscr.refresh()
            return

        now = datetime.now().astimezone()
        self._render_metrics_strip(stdscr, row=0, width=width, now=now)

        metrics_separator_row = 1
        shell_row = height - 1
        shell_separator_row = max(metrics_separator_row + 1, shell_row - 1)

        if metrics_separator_row < height:
            self._draw_separator(stdscr, metrics_separator_row, width)
        if 0 <= shell_separator_row < height and shell_separator_row != metrics_separator_row:
            self._draw_separator(stdscr, shell_separator_row, width)

        log_top = metrics_separator_row + 1
        log_bottom = shell_separator_row - 1
        log_height = max(0, log_bottom - log_top + 1)
        self._render_log(stdscr, top=log_top, height=log_height, width=width)
        self._render_shell_input(stdscr, row=shell_row, width=width)

        stdscr.refresh()

    def _render_metrics_strip(
        self,
        stdscr: "curses._CursesWindow",
        *,
        row: int,
        width: int,
        now: datetime,
    ) -> None:
        """Render the one-line metrics strip with priority-based truncation."""
        if row < 0:
            return

        spinner_frame = SPINNER_FRAMES[self._spinner_index]
        self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)

        turn_counts = _derive_turn_counts(self._entries)
        thinking_total = _derive_completed_thinking_seconds(self._entries) + _derive_inflight_thinking_seconds(
            self._metrics, now=now
        )
        status_text, thinking_agent = _status_text(self._metrics, spinner_frame=spinner_frame)
        mode_text = _mode_text(self._metrics)
        uptime_text = _uptime_text(self._metrics, now=now)

        separator_attr = curses.A_DIM
        mode_attr = (
            self._with_optional_color(PAIR_MODE)
            if mode_text == "collaborative"
            else self._with_optional_color(PAIR_SHELL)
        )
        if thinking_agent == "claude":
            status_attr = self._with_optional_color(PAIR_CLAUDE)
        elif thinking_agent == "codex":
            status_attr = self._with_optional_color(PAIR_CODEX)
        elif thinking_agent == "both":
            status_attr = curses.A_NORMAL
        else:
            status_attr = curses.A_DIM

        required_groups: list[list[tuple[str, int]]] = [
            [(status_text, status_attr)],
            [(" | ", separator_attr), (mode_text, mode_attr)],
        ]
        optional_groups: list[list[tuple[str, int]]] = [
            [(" | ", separator_attr), (f"think {_format_elapsed(thinking_total)}", curses.A_DIM)],
            [(" | ", separator_attr), (f"up {uptime_text}", curses.A_DIM)],
            [(" | ", separator_attr), (f"claude:{turn_counts['claude']}", self._with_optional_color(PAIR_CLAUDE))],
            [(" | ", separator_attr), (f"codex:{turn_counts['codex']}", self._with_optional_color(PAIR_CODEX))],
        ]

        display_width = max(0, width - 1)
        groups = required_groups + optional_groups
        while _segment_groups_width(groups) > display_width and len(groups) > len(required_groups):
            groups.pop()

        segments = [segment for group in groups for segment in group]
        self._draw_segments(stdscr, row=row, width=width, segments=segments)

    def _render_log(self, stdscr: "curses._CursesWindow", *, top: int, height: int, width: int) -> None:
        """Render tail of wrapped log lines in available space."""
        if height <= 0 or top < 0:
            return
        self._last_log_height = height

        log_width = max(1, width - 1) if width > 1 else 1
        wrapped = self._wrapped_log_lines(log_width)
        max_scroll = max(0, len(wrapped) - height)
        if self._scroll_offset > max_scroll:
            self._scroll_offset = max_scroll
        end_index = len(wrapped) - self._scroll_offset
        start_index = max(0, end_index - height)
        visible = wrapped[start_index:end_index]
        start_row = top + max(0, height - len(visible))
        for index, (line, attr) in enumerate(visible):
            row = start_row + index
            if row < 0:
                continue
            self._draw_line(stdscr, row, line, log_width, attr)

        if width > 1 and max_scroll > 0:
            self._draw_scrollbar(
                stdscr,
                top=top,
                height=height,
                column=width - 1,
                scroll_offset=self._scroll_offset,
                max_scroll=max_scroll,
                total_lines=len(wrapped),
            )

    @staticmethod
    def _draw_scrollbar(
        stdscr: "curses._CursesWindow",
        *,
        top: int,
        height: int,
        column: int,
        scroll_offset: int,
        max_scroll: int,
        total_lines: int,
    ) -> None:
        """Draw a proportional scrollbar in a fixed right-side column."""
        if height <= 0 or max_scroll <= 0 or total_lines <= 0:
            return

        thumb_size = max(1, min(height, int(round((height * height) / total_lines))))
        travel = max(0, height - thumb_size)
        # scroll_offset=0 means tail (newest logs), so thumb belongs at bottom
        thumb_top = (
            int(round(((max_scroll - scroll_offset) / max_scroll) * travel))
            if travel > 0
            else 0
        )
        thumb_bottom = thumb_top + thumb_size

        for offset in range(height):
            row = top + offset
            try:
                stdscr.addch(row, column, curses.ACS_VLINE, curses.A_DIM)
            except curses.error:
                continue
            if thumb_top <= offset < thumb_bottom:
                try:
                    stdscr.addch(row, column, curses.ACS_VLINE, curses.A_BOLD)
                except curses.error:
                    continue

    def _render_shell_input(self, stdscr: "curses._CursesWindow", *, row: int, width: int) -> None:
        """Render shell prompt and move cursor."""
        if row < 0:
            return
        prompt = "$ "
        visible_width = max(0, width - len(prompt))
        display_buffer = self._input_buffer
        if len(display_buffer) > visible_width:
            display_buffer = display_buffer[-visible_width:]
        line = f"{prompt}{display_buffer}"
        self._draw_line(stdscr, row, line, width, curses.A_NORMAL)

        cursor_col = min(max(0, width - 1), len(prompt) + len(display_buffer))
        try:
            stdscr.move(row, cursor_col)
        except curses.error:
            pass

    def _wrapped_log_lines(self, width: int) -> list[tuple[str, int]]:
        """Build wrapped render lines from log entries."""
        wrapped: list[tuple[str, int]] = []
        for entry in self._entries:
            timestamp = entry.timestamp.astimezone().strftime("%H:%M:%S")
            kind_padding = " " * max(0, 6 - len(entry.kind))
            kind_block = f"{kind_padding}[{entry.kind}]"
            prefix = f"{timestamp} {kind_block} "
            chunks = textwrap.wrap(
                entry.message,
                width=max(1, width - len(prefix)),
                replace_whitespace=False,
                drop_whitespace=False,
            )
            if not chunks:
                chunks = [""]
            attr = self._entry_attr(entry)
            wrapped.append((f"{prefix}{chunks[0]}", attr))
            continuation_prefix = " " * len(prefix)
            for chunk in chunks[1:]:
                wrapped.append((f"{continuation_prefix}{chunk}", attr))
        return wrapped

    def _entry_attr(self, entry: LogEntry) -> int:
        """Return curses attributes for one log entry."""
        pair = 0
        if entry.kind == "error":
            pair = PAIR_ERROR
        elif entry.kind == "shell":
            pair = PAIR_SHELL
        elif entry.kind in {"system", "status"}:
            pair = PAIR_SYSTEM
        else:
            reference = " ".join(
                part for part in (entry.agent, entry.target, entry.message.lower()) if part
            )
            if "codex" in reference:
                pair = PAIR_CODEX
            elif "claude" in reference:
                pair = PAIR_CLAUDE

        attr = curses.A_NORMAL
        if self._colors_enabled and pair:
            attr |= curses.color_pair(pair)
        if entry.kind in {"system", "status"}:
            attr |= curses.A_DIM
        return attr

    @staticmethod
    def _draw_separator(stdscr: "curses._CursesWindow", row: int, width: int) -> None:
        """Draw one horizontal separator line."""
        try:
            stdscr.hline(row, 0, curses.ACS_HLINE, max(0, width))
        except curses.error:
            pass

    @staticmethod
    def _draw_line(
        stdscr: "curses._CursesWindow",
        row: int,
        text: str,
        width: int,
        attr: int,
    ) -> None:
        """Draw a clipped line at fixed row."""
        if row < 0:
            return
        clipped = text[: max(0, width - 1)]
        try:
            stdscr.addnstr(row, 0, clipped, max(0, width - 1), attr)
        except curses.error:
            pass

    def _with_optional_color(self, pair_id: int, *, bold: bool = False) -> int:
        """Build an attribute mask with optional color support."""
        attr = curses.A_BOLD if bold else curses.A_NORMAL
        if self._colors_enabled:
            attr |= curses.color_pair(pair_id)
        return attr

    @staticmethod
    def _draw_segments(
        stdscr: "curses._CursesWindow",
        *,
        row: int,
        width: int,
        segments: list[tuple[str, int]],
    ) -> None:
        """Draw one row from colored text segments."""
        if row < 0:
            return
        max_width = max(0, width - 1)
        column = 0
        for text, attr in segments:
            if not text or column >= max_width:
                break
            remaining = max_width - column
            clipped = text[:remaining]
            try:
                stdscr.addnstr(row, column, clipped, remaining, attr)
            except curses.error:
                continue
            column += len(clipped)


def _parse_event_line(raw_line: str) -> LogEntry | None:
    """Parse one JSONL event line into a sidebar log entry."""
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    kind = payload.get("kind")
    message = payload.get("message")
    if not isinstance(kind, str) or not isinstance(message, str):
        return None

    timestamp_raw = payload.get("ts")
    if not isinstance(timestamp_raw, str):
        return None
    timestamp = _parse_iso8601(timestamp_raw)
    if timestamp is None:
        return None

    agent = payload.get("agent")
    target = payload.get("target")
    return LogEntry(
        timestamp=timestamp,
        kind=kind,
        message=message,
        agent=agent if isinstance(agent, str) else None,
        target=target if isinstance(target, str) else None,
    )


def _parse_iso8601(value: str) -> datetime | None:
    """Parse ISO timestamp value with timezone support."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _default_metrics_snapshot() -> dict[str, Any]:
    """Return schema-compatible default metrics values."""
    return {
        "target": "claude",
        "mode": "normal",
        "collab_turn": None,
        "collab_max": None,
        "uptime_start": None,
        "agents": {
            "claude": {
                "status": "idle",
                "thinking_since": None,
                "last_words": None,
                "last_latency_s": None,
            },
            "codex": {
                "status": "idle",
                "thinking_since": None,
                "last_words": None,
                "last_latency_s": None,
            },
        },
    }


def _load_metrics_snapshot(path: Path, current: dict[str, Any]) -> dict[str, Any]:
    """Load metrics snapshot, falling back to current data when invalid."""
    if not path.exists():
        return current
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return current
    if not isinstance(payload, dict):
        return current

    merged = json.loads(json.dumps(_default_metrics_snapshot()))
    _merge_known_fields(merged, payload)
    return merged


def _merge_known_fields(destination: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge only known keys from source into destination."""
    for key, value in source.items():
        if key not in destination:
            continue
        current = destination[key]
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_known_fields(current, value)
            continue
        destination[key] = value


def _mode_text(metrics: dict[str, Any]) -> str:
    """Return normalized mode text for the strip."""
    mode = metrics.get("mode")
    if mode == "collab":
        return "collaborative"
    return str(mode or "normal")


def _uptime_text(metrics: dict[str, Any], *, now: datetime) -> str:
    """Return uptime text derived from metrics uptime_start."""
    uptime_start = metrics.get("uptime_start")
    if not isinstance(uptime_start, str):
        return _format_elapsed(0.0)
    parsed = _parse_iso8601(uptime_start)
    if parsed is None:
        return _format_elapsed(0.0)
    elapsed = (now - parsed.astimezone(now.tzinfo or timezone.utc)).total_seconds()
    return _format_elapsed(elapsed)


def _status_text(metrics: dict[str, Any], *, spinner_frame: str) -> tuple[str, str | None]:
    """Return strip status text and active thinking agent label."""
    active_agent = _active_thinking_agent(metrics)
    if active_agent is None:
        return ". idle", None
    if active_agent == "both":
        return f"{spinner_frame} both", "both"
    return f"{spinner_frame} {active_agent}", active_agent


def _active_thinking_agent(metrics: dict[str, Any]) -> str | None:
    """Return the active thinking agent name, both, or none."""
    agents = metrics.get("agents")
    if not isinstance(agents, dict):
        return None

    thinking_agents: list[str] = []
    for agent in ("claude", "codex"):
        data = agents.get(agent)
        if isinstance(data, dict) and data.get("status") == "thinking":
            thinking_agents.append(agent)

    if not thinking_agents:
        return None
    if len(thinking_agents) == 1:
        return thinking_agents[0]

    # prefer the most recently started thinking agent if timestamps are valid
    latest_agent: str | None = None
    latest_time: datetime | None = None
    for agent in thinking_agents:
        data = agents.get(agent)
        if not isinstance(data, dict):
            continue
        raw_since = data.get("thinking_since")
        if not isinstance(raw_since, str):
            continue
        parsed_since = _parse_iso8601(raw_since)
        if parsed_since is None:
            continue
        if latest_time is None or parsed_since > latest_time:
            latest_time = parsed_since
            latest_agent = agent

    return latest_agent or "both"


def _derive_turn_counts(entries: Iterable[LogEntry]) -> dict[str, int]:
    """Count completed turns per agent from recv events."""
    counts = {"claude": 0, "codex": 0}
    for entry in entries:
        if entry.kind == "recv" and entry.agent in counts:
            counts[entry.agent] += 1
    return counts


def _derive_completed_thinking_seconds(entries: Iterable[LogEntry]) -> float:
    """Sum thinking durations by pairing sent(target) to recv(agent)."""
    sent_starts: dict[str, datetime] = {}
    total = 0.0
    for entry in entries:
        if entry.kind == "sent" and entry.target in {"claude", "codex"}:
            sent_starts[entry.target] = entry.timestamp
            continue
        if entry.kind != "recv" or entry.agent not in {"claude", "codex"}:
            continue
        start = sent_starts.pop(entry.agent, None)
        if start is None:
            continue
        duration = (entry.timestamp - start).total_seconds()
        if duration > 0:
            total += duration
    return total


def _derive_inflight_thinking_seconds(metrics: dict[str, Any], *, now: datetime) -> float:
    """Return current in-flight thinking time from metrics thinking_since."""
    agents = metrics.get("agents")
    if not isinstance(agents, dict):
        return 0.0

    total = 0.0
    for agent in ("claude", "codex"):
        data = agents.get(agent)
        if not isinstance(data, dict) or data.get("status") != "thinking":
            continue
        raw_since = data.get("thinking_since")
        if not isinstance(raw_since, str):
            continue
        parsed_since = _parse_iso8601(raw_since)
        if parsed_since is None:
            continue
        elapsed = (now - parsed_since.astimezone(now.tzinfo or timezone.utc)).total_seconds()
        if elapsed > 0:
            total += elapsed
    return total


def _segment_groups_width(groups: Iterable[Iterable[tuple[str, int]]]) -> int:
    """Return display width for grouped segments."""
    width = 0
    for group in groups:
        for text, _attr in group:
            width += len(text)
    return width


def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as compact uptime text."""
    bounded = max(0, int(seconds))
    hours, remainder = divmod(bounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _looks_interactive_command(command: str) -> bool:
    """Return true for known interactive shell commands."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    binary = Path(tokens[0]).name
    return binary in INTERACTIVE_COMMANDS


def _collect_capped_output(
    *,
    stdout: str,
    stderr: str,
    max_lines: int,
    max_bytes: int,
) -> tuple[list[str], bool]:
    """Collect stdout/stderr lines with line and byte caps."""
    combined: list[str] = []
    if stdout:
        combined.extend(stdout.splitlines())
    if stderr:
        combined.extend(f"[stderr] {line}" for line in stderr.splitlines())

    lines: list[str] = []
    total_bytes = 0
    truncated = False
    for line in combined:
        line_bytes = len(line.encode("utf-8")) + 1
        if len(lines) >= max_lines or total_bytes + line_bytes > max_bytes:
            truncated = True
            break
        lines.append(line)
        total_bytes += line_bytes
    return lines, truncated


def _as_text(value: str | bytes | None) -> str:
    """Convert subprocess output values to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def run_sidebar(workspace_root: Path) -> int:
    """Run sidebar application for one workspace."""
    return SidebarApplication(workspace_root=workspace_root).run()

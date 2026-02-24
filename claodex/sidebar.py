"""Curses sidebar for claodex metrics, logs, and shell input."""

from __future__ import annotations

import curses
import json
import shlex
import subprocess
import textwrap
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .state import ui_events_file, ui_metrics_file

METRICS_POLL_SECONDS = 0.5
LOOP_TIMEOUT_MILLISECONDS = 100
LOG_BUFFER_MAX = 4000
SHELL_TIMEOUT_SECONDS = 30
SHELL_MAX_LINES = 100
SHELL_MAX_BYTES = 10 * 1024
INTERACTIVE_COMMANDS = frozenset({"vim", "nvim", "nano", "less", "more", "top", "htop"})

PAIR_CODEX = 1
PAIR_CLAUDE = 2
PAIR_ERROR = 3
PAIR_SHELL = 4
PAIR_SYSTEM = 5


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
        self._colors_enabled = False

    def run(self) -> int:
        """Run sidebar app until interrupted."""
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
            claude_color = 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW
            curses.init_pair(PAIR_CODEX, curses.COLOR_BLUE, -1)
            curses.init_pair(PAIR_CLAUDE, claude_color, -1)
            curses.init_pair(PAIR_ERROR, curses.COLOR_RED, -1)
            curses.init_pair(PAIR_SHELL, curses.COLOR_CYAN, -1)
            curses.init_pair(PAIR_SYSTEM, curses.COLOR_WHITE, -1)
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
                timestamp=datetime.now(timezone.utc),
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

        metrics_lines = _format_metrics_lines(self._metrics, now=datetime.now(timezone.utc))
        metrics_height = min(3, max(1, height - 2))
        for row in range(min(metrics_height, len(metrics_lines), height)):
            self._draw_line(stdscr, row, metrics_lines[row], width, curses.A_BOLD if row == 0 else 0)

        metrics_separator_row = metrics_height
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

    def _render_log(self, stdscr: "curses._CursesWindow", *, top: int, height: int, width: int) -> None:
        """Render tail of wrapped log lines in available space."""
        if height <= 0 or top < 0:
            return

        wrapped = self._wrapped_log_lines(max(1, width))
        visible = wrapped[-height:]
        start_row = top + max(0, height - len(visible))
        for index, (line, attr) in enumerate(visible):
            row = start_row + index
            if row < 0:
                continue
            self._draw_line(stdscr, row, line, width, attr)

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
            timestamp = entry.timestamp.strftime("%H:%M:%S")
            base_line = f"{timestamp} [{entry.kind}] {entry.message}"
            chunks = textwrap.wrap(
                base_line,
                width=max(1, width),
                replace_whitespace=False,
                drop_whitespace=False,
            )
            if not chunks:
                chunks = [""]
            attr = self._entry_attr(entry)
            wrapped.extend((chunk, attr) for chunk in chunks)
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


def _format_metrics_lines(metrics: dict[str, Any], *, now: datetime) -> list[str]:
    """Build fixed metrics-strip lines."""
    target = metrics.get("target")
    mode = metrics.get("mode")
    collab_turn = metrics.get("collab_turn")
    collab_max = metrics.get("collab_max")
    uptime_start = metrics.get("uptime_start")
    uptime = _format_elapsed(0.0)
    if isinstance(uptime_start, str):
        parsed = _parse_iso8601(uptime_start)
        if parsed is not None:
            uptime = _format_elapsed((now - parsed.astimezone(now.tzinfo or timezone.utc)).total_seconds())

    if mode == "collab" and isinstance(collab_turn, int) and isinstance(collab_max, int):
        mode_text = f"collab {collab_turn}/{collab_max}"
    elif mode == "collab" and isinstance(collab_max, int):
        mode_text = f"collab ?/{collab_max}"
    else:
        mode_text = str(mode or "normal")

    agents = metrics.get("agents")
    if not isinstance(agents, dict):
        agents = {}
    claude = agents.get("claude", {})
    codex = agents.get("codex", {})
    if not isinstance(claude, dict):
        claude = {}
    if not isinstance(codex, dict):
        codex = {}

    line_one = f"target: {target or 'claude'} | mode: {mode_text} | uptime: {uptime}"
    line_two = f"claude: {_agent_status_text(claude, now)} | codex: {_agent_status_text(codex, now)}"
    line_three = (
        "last: "
        f"claude {_last_stats_text(claude)} | "
        f"codex {_last_stats_text(codex)}"
    )
    return [line_one, line_two, line_three]


def _agent_status_text(agent_metrics: dict[str, Any], now: datetime) -> str:
    """Format one agent status line segment."""
    status = agent_metrics.get("status")
    if status != "thinking":
        return "idle"

    thinking_since = agent_metrics.get("thinking_since")
    if isinstance(thinking_since, str):
        parsed = _parse_iso8601(thinking_since)
        if parsed is not None:
            elapsed = (now - parsed.astimezone(now.tzinfo or timezone.utc)).total_seconds()
            return f"thinking {_format_elapsed(elapsed)}"
    return "thinking"


def _last_stats_text(agent_metrics: dict[str, Any]) -> str:
    """Format last words/latency metrics for one agent."""
    words = agent_metrics.get("last_words")
    latency = agent_metrics.get("last_latency_s")

    words_text = f"{words}w" if isinstance(words, int) else "-w"
    if isinstance(latency, (int, float)):
        latency_text = f"{latency:.1f}s"
    else:
        latency_text = "-s"
    return f"{words_text} {latency_text}"


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

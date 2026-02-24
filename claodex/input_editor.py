"""Minimal interactive input editor for claodex REPL."""

from __future__ import annotations

import codecs
import os
import re
import select
import sys
import termios
import tty
from dataclasses import dataclass

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _colored_prompt(target: str) -> str:
    """Return ANSI-colored prompt for the current target."""
    if target == "claude":
        return "\033[38;5;216mclaude ❯ │ \033[0m"
    if target == "codex":
        return "\033[38;5;116m codex ❯ │ \033[0m"
    if target == "collab":
        return "\033[90mcollab ❯ │ \033[0m"
    return f"{target} ❯ │ "


def _visible_len(value: str) -> int:
    """Return display width excluding ANSI escape sequences."""
    return len(ANSI_ESCAPE_RE.sub("", value))


def _terminal_columns(default: int = 80) -> int:
    """Return terminal width, falling back when unavailable."""
    try:
        return max(1, os.get_terminal_size().columns)
    except OSError:
        return max(1, default)


@dataclass(frozen=True)
class InputEvent:
    """One REPL input event from the terminal editor."""

    kind: str
    value: str = ""


@dataclass(frozen=True)
class _VisualRow:
    """One rendered visual row mapped to buffer offsets."""

    start: int
    length: int


@dataclass(frozen=True)
class _VisualLayout:
    """Shared prompt/buffer visual layout for render and cursor movement."""

    text: str
    lines: list[str]
    segmented_lines: list[list[str]]
    visual_per_line: list[int]
    visual_rows: list[_VisualRow]
    prompt_width: int
    continuation_prefix: str
    usable_per_row: int


class InputEditor:
    """Simple raw-mode line editor with Tab toggle and Ctrl+J newlines."""

    def __init__(self) -> None:
        """Initialize editor state."""
        self._history: list[str] = []

    def read(
        self,
        target: str,
        on_idle: "Callable[[], InputEvent | None] | None" = None,
        idle_interval: float = 0.2,
        prefill: str = "",
    ) -> InputEvent:
        """Read one input event.

        Args:
            target: Current target label shown in prompt.
            on_idle: Optional callback invoked during idle periods (when no
                keystrokes arrive within *idle_interval*). If it returns an
                InputEvent, that event is yielded immediately. While inside
                bracketed paste, idle callbacks are suppressed.
            idle_interval: Seconds between idle callback invocations.
            prefill: Optional text to pre-populate the input buffer with.

        Returns:
            InputEvent for submitted text or control action.
        """
        prompt = _colored_prompt(target)
        buffer: list[str] = list(prefill)
        cursor = len(buffer)
        history_index: int | None = None
        pasting = False
        # (total visual rows, cursor's visual row from top of render)
        previous_render = (1, 0)

        self._write(prompt)
        if buffer:
            previous_render = self._render(prompt, buffer, cursor, previous_render)

        with _raw_terminal_mode(sys.stdin.fileno()):
            # enable bracketed paste so the terminal wraps pasted text in
            # \x1b[200~ ... \x1b[201~ — inside the bracket, \r becomes a
            # literal newline instead of submit
            self._write("\x1b[?2004h")
            try:
                return self._read_loop(
                    prompt, buffer, cursor, history_index, pasting, previous_render,
                    on_idle=on_idle, idle_interval=idle_interval,
                )
            finally:
                self._write("\x1b[?2004l")

    def _read_loop(
        self,
        prompt: str,
        buffer: list[str],
        cursor: int,
        history_index: int | None,
        pasting: bool,
        previous_render: tuple[int, int],
        on_idle: "Callable[[], InputEvent | None] | None" = None,
        idle_interval: float = 0.2,
    ) -> InputEvent:
        """Core input loop, split out so bracketed paste cleanup is guaranteed.

        Args:
            prompt: Prompt string.
            buffer: Character buffer.
            cursor: Cursor position.
            history_index: Current history navigation index.
            pasting: Whether we are inside a bracketed paste.
            previous_render: Previous render state.
            on_idle: Optional idle callback.
            idle_interval: Seconds between idle polls.

        Returns:
            InputEvent for submitted text or control action.
        """
        decoder = codecs.getincrementaldecoder("utf-8")()
        fd = sys.stdin.fileno()
        last_columns = _terminal_columns()
        while True:
            # keep render and wrap model in sync with terminal resizes
            current_columns = _terminal_columns()
            if current_columns != last_columns:
                last_columns = current_columns
                previous_render = self._render(prompt, buffer, cursor, previous_render)

            # use select with timeout to enable idle polling
            ready, _, _ = select.select([sys.stdin], [], [], idle_interval)
            if not ready:
                # no keystroke — invoke idle callback unless we are inside
                # bracketed paste, where early interruption can split the
                # paste stream and corrupt the next read cycle
                if on_idle is not None and not pasting:
                    event = on_idle()
                    if event is not None:
                        # if auto-collab interrupts while the user has a
                        # draft, stash it in event.value for later restore
                        if buffer:
                            self._clear_render(previous_render)
                            self._write("\r\n")
                            event = InputEvent(kind=event.kind, value="".join(buffer))
                        return event
                continue

            char = os.read(fd, 1)
            if not char:
                return InputEvent(kind="quit")

            key = _decode_utf8_key(decoder, char)
            if key is None:
                continue

            if key == "\t":
                if pasting:
                    # preserve literal tabs from pasted content
                    buffer.insert(cursor, "\t")
                    cursor += 1
                    previous_render = self._render(prompt, buffer, cursor, previous_render)
                    continue
                self._clear_render(previous_render)
                return InputEvent(kind="toggle")

            if key == "\x03" and not pasting:
                raise KeyboardInterrupt

            if key == "\x04" and not pasting:
                self._write("\r\n")
                return InputEvent(kind="quit")

            # ctrl+j (0x0a / \n) inserts a newline; during paste, \r also
            # inserts a newline instead of submitting
            if key == "\x0a" or (key == "\r" and pasting):
                buffer.insert(cursor, "\n")
                cursor += 1
                previous_render = self._render(prompt, buffer, cursor, previous_render)
                continue

            # carriage return submits (Enter sends \r in raw mode)
            if key == "\r":
                text = "".join(buffer)
                self._write("\r\n")
                columns = _terminal_columns()
                separator = "─" * max(1, columns - 1)
                self._write(f"\r\x1b[2K\033[90m{separator}\033[0m\r\n")
                if text.strip():
                    self._history.append(text)
                return InputEvent(kind="submit", value=text)

            if key in {"\x7f", "\b"}:
                if cursor > 0:
                    del buffer[cursor - 1]
                    cursor -= 1
                previous_render = self._render(prompt, buffer, cursor, previous_render)
                continue

            # escape sequences: during paste, only bracket boundaries are
            # handled — other ANSI sequences (color codes, cursor movement,
            # etc.) are intentionally consumed and dropped to prevent raw
            # terminal control codes from corrupting the buffer
            if key == "\x1b":
                sequence = self._read_escape_sequence()

                # bracketed paste boundaries
                if sequence == "[200~":
                    pasting = True
                    continue
                if sequence == "[201~":
                    pasting = False
                    continue

                if sequence in {"[C", "OC"}:  # right
                    cursor = min(cursor + 1, len(buffer))
                elif sequence in {"[D", "OD"}:  # left
                    cursor = max(cursor - 1, 0)
                elif sequence in {"[A", "OA"}:  # up
                    layout = self._visual_layout(prompt, buffer)
                    if len(layout.visual_rows) > 1:
                        cursor = self._move_cursor_by_visual_row(layout, cursor, -1)
                    elif self._history:
                        # single-line: history back
                        if history_index is None:
                            history_index = len(self._history) - 1
                        else:
                            history_index = max(0, history_index - 1)
                        buffer = list(self._history[history_index])
                        cursor = len(buffer)
                elif sequence in {"[B", "OB"}:  # down
                    layout = self._visual_layout(prompt, buffer)
                    if len(layout.visual_rows) > 1:
                        cursor = self._move_cursor_by_visual_row(layout, cursor, 1)
                    elif self._history:
                        # single-line: history forward
                        if history_index is None:
                            pass
                        elif history_index >= len(self._history) - 1:
                            history_index = None
                            buffer = []
                            cursor = 0
                        else:
                            history_index += 1
                            buffer = list(self._history[history_index])
                            cursor = len(buffer)
                elif sequence in {"[H", "OH", "[1~"}:  # home
                    cursor = 0
                elif sequence in {"[F", "OF", "[4~"}:  # end
                    cursor = len(buffer)
                elif sequence == "[3~":  # delete (forward)
                    if cursor < len(buffer):
                        del buffer[cursor]
                previous_render = self._render(prompt, buffer, cursor, previous_render)
                continue

            # printable characters
            if key and key >= " ":
                buffer.insert(cursor, key)
                cursor += 1
                previous_render = self._render(prompt, buffer, cursor, previous_render)

    def _visual_layout(self, prompt: str, buffer: list[str]) -> _VisualLayout:
        """Build one visual layout shared by render and cursor movement."""
        text = "".join(buffer)
        lines = text.split("\n")
        if not lines:
            lines = [""]

        prompt_width = _visible_len(prompt)
        continuation_prefix = " " * prompt_width
        columns = _terminal_columns()
        usable_per_row = max(1, columns - prompt_width)

        segmented_lines: list[list[str]] = []
        visual_rows: list[_VisualRow] = []
        visual_per_line: list[int] = []
        line_start = 0

        for line_index, line in enumerate(lines):
            if not line:
                segments = [""]
            else:
                segments = [
                    line[index : index + usable_per_row]
                    for index in range(0, len(line), usable_per_row)
                ]
            segmented_lines.append(segments)
            visual_per_line.append(len(segments))

            for segment_index, segment in enumerate(segments):
                segment_start = line_start + (segment_index * usable_per_row)
                visual_rows.append(_VisualRow(start=segment_start, length=len(segment)))

            line_start += len(line)
            if line_index < len(lines) - 1:
                line_start += 1

        return _VisualLayout(
            text=text,
            lines=lines,
            segmented_lines=segmented_lines,
            visual_per_line=visual_per_line,
            visual_rows=visual_rows,
            prompt_width=prompt_width,
            continuation_prefix=continuation_prefix,
            usable_per_row=usable_per_row,
        )

    def _cursor_to_visual_position(self, layout: _VisualLayout, cursor: int) -> tuple[int, int]:
        """Map cursor index to visual (row, col) using layout wrap rules."""
        bounded_cursor = min(max(0, cursor), len(layout.text))
        before_cursor = layout.text[:bounded_cursor]
        cursor_line = before_cursor.count("\n")
        cursor_col_in_line = len(before_cursor.split("\n")[-1])
        cursor_line_length = len(layout.lines[cursor_line])
        cursor_segments = layout.segmented_lines[cursor_line]

        if (
            cursor_col_in_line == cursor_line_length
            and cursor_col_in_line > 0
            and cursor_col_in_line % layout.usable_per_row == 0
        ):
            # keep boundary-at-end positions on the last occupied row
            cursor_segment_index = len(cursor_segments) - 1
            cursor_segment_col = layout.usable_per_row
        else:
            cursor_segment_index = cursor_col_in_line // layout.usable_per_row
            cursor_segment_col = cursor_col_in_line % layout.usable_per_row

        cursor_visual_row = sum(layout.visual_per_line[:cursor_line]) + cursor_segment_index
        return cursor_visual_row, cursor_segment_col

    @staticmethod
    def _visual_position_to_cursor(layout: _VisualLayout, row: int, col: int) -> int:
        """Map visual (row, col) back to cursor index."""
        visual_row = layout.visual_rows[row]
        clamped_col = min(max(0, col), visual_row.length)
        return visual_row.start + clamped_col

    def _move_cursor_by_visual_row(self, layout: _VisualLayout, cursor: int, step: int) -> int:
        """Move cursor one visual row up/down while preserving visual column."""
        current_row, current_col = self._cursor_to_visual_position(layout, cursor)
        target_row = min(max(0, current_row + step), len(layout.visual_rows) - 1)
        return self._visual_position_to_cursor(layout, target_row, current_col)

    def _read_escape_sequence(self) -> str:
        """Read a simple ANSI escape sequence body."""
        first = os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
        if first != "[" and first != "O":
            return first

        sequence = first
        for _ in range(8):
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not ready:
                return sequence
            char = os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
            if not char:
                return sequence
            sequence += char
            if char.isalpha() or char == "~":
                return sequence
        return sequence

    def _render(
        self,
        prompt: str,
        buffer: list[str],
        cursor: int,
        previous_render: tuple[int, int],
    ) -> tuple[int, int]:
        """Redraw prompt + buffer and reposition the cursor.

        Args:
            prompt: Prompt string.
            buffer: Current input buffer characters.
            cursor: Cursor position in buffer.
            previous_render: (total visual rows, cursor visual row) from last render.

        Returns:
            (total visual rows, cursor visual row) for this render.
        """
        prev_total, prev_cursor_row = previous_render
        layout = self._visual_layout(prompt, buffer)
        visual_lines = len(layout.visual_rows)

        # move from cursor's current visual row to render start, clear, reset
        self._move_up(prev_cursor_row)
        self._clear_n_lines(prev_total)
        self._move_up(prev_total - 1)

        for line_index, segments in enumerate(layout.segmented_lines):
            for segment_index, segment in enumerate(segments):
                if line_index == 0 and segment_index == 0:
                    self._write(f"{prompt}{segment}")
                else:
                    self._write(f"\r\n{layout.continuation_prefix}{segment}")

        # position cursor using the shared layout model
        cursor_visual_row, cursor_segment_col = self._cursor_to_visual_position(layout, cursor)
        cursor_column = layout.prompt_width + cursor_segment_col

        # move up from the last visual row to the cursor's visual row
        rows_up = (visual_lines - 1) - cursor_visual_row
        if rows_up > 0:
            self._write(f"\x1b[{rows_up}A")
        self._write("\r")
        if cursor_column > 0:
            self._write(f"\x1b[{cursor_column}C")
        return visual_lines, cursor_visual_row

    def _clear_render(self, previous_render: tuple[int, int]) -> None:
        """Clear the current input render from the terminal."""
        prev_total, prev_cursor_row = previous_render
        self._move_up(prev_cursor_row)
        self._clear_n_lines(prev_total)
        self._move_up(prev_total - 1)

    def _move_up(self, rows: int) -> None:
        """Move cursor to column 0, then up by the given number of rows."""
        self._write("\r")
        if rows > 0:
            self._write(f"\x1b[{rows}A")

    def _clear_n_lines(self, count: int) -> None:
        """Clear a fixed number of terminal lines from current position."""
        for index in range(count):
            self._write("\x1b[2K")
            if index < count - 1:
                self._write("\x1b[1B\r")

    @staticmethod
    def _write(content: str) -> None:
        """Write content to stdout immediately."""
        sys.stdout.write(content)
        sys.stdout.flush()


class _raw_terminal_mode:
    """Context manager for raw terminal mode."""

    def __init__(self, file_descriptor: int) -> None:
        self.file_descriptor = file_descriptor
        self._saved: list[int] | None = None

    def __enter__(self) -> None:
        self._saved = termios.tcgetattr(self.file_descriptor)
        tty.setraw(self.file_descriptor)

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        if self._saved is not None:
            termios.tcsetattr(self.file_descriptor, termios.TCSADRAIN, self._saved)


def _decode_utf8_key(decoder: codecs.IncrementalDecoder, chunk: bytes) -> str | None:
    """Decode one UTF-8 key chunk from raw terminal bytes.

    Args:
        decoder: Incremental UTF-8 decoder instance.
        chunk: Raw byte chunk from terminal input.

    Returns:
        Decoded key string when complete, otherwise None.
    """
    try:
        key = decoder.decode(chunk, final=False)
    except UnicodeDecodeError:
        decoder.reset()
        return None
    if key == "":
        return None
    return key

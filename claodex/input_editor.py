"""Minimal interactive input editor for claodex REPL."""

from __future__ import annotations

import codecs
import os
import select
import sys
import termios
import tty
from dataclasses import dataclass


@dataclass(frozen=True)
class InputEvent:
    """One REPL input event from the terminal editor."""

    kind: str
    value: str = ""


class InputEditor:
    """Simple raw-mode line editor with Tab toggle and Ctrl+J newlines."""

    def __init__(self) -> None:
        """Initialize editor state."""
        self._history: list[str] = []

    def read(self, target: str) -> InputEvent:
        """Read one input event.

        Args:
            target: Current target label shown in prompt.

        Returns:
            InputEvent for submitted text or control action.
        """
        prompt = f"{target} ❯ "
        buffer: list[str] = []
        cursor = 0
        history_index: int | None = None
        pasting = False
        # (total visual rows, cursor's visual row from top of render)
        previous_render = (1, 0)

        self._write(prompt)

        with _raw_terminal_mode(sys.stdin.fileno()):
            # enable bracketed paste so the terminal wraps pasted text in
            # \x1b[200~ ... \x1b[201~ — inside the bracket, \r becomes a
            # literal newline instead of submit
            self._write("\x1b[?2004h")
            try:
                return self._read_loop(
                    prompt, buffer, cursor, history_index, pasting, previous_render
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
    ) -> InputEvent:
        """Core input loop, split out so bracketed paste cleanup is guaranteed.

        Args:
            prompt: Prompt string.
            buffer: Character buffer.
            cursor: Cursor position.
            history_index: Current history navigation index.
            pasting: Whether we are inside a bracketed paste.
            previous_render: Previous render state.

        Returns:
            InputEvent for submitted text or control action.
        """
        decoder = codecs.getincrementaldecoder("utf-8")()
        while True:
            char = os.read(sys.stdin.fileno(), 1)
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
                    text = "".join(buffer)
                    if "\n" in text:
                        # multi-line: move cursor up one logical line
                        before = text[:cursor]
                        line_idx = before.count("\n")
                        col = len(before.split("\n")[-1])
                        if line_idx == 0:
                            cursor = 0
                        else:
                            lines = text.split("\n")
                            target_col = min(col, len(lines[line_idx - 1]))
                            cursor = sum(len(lines[i]) + 1 for i in range(line_idx - 1)) + target_col
                    elif self._history:
                        # single-line: history back
                        if history_index is None:
                            history_index = len(self._history) - 1
                        else:
                            history_index = max(0, history_index - 1)
                        buffer = list(self._history[history_index])
                        cursor = len(buffer)
                elif sequence in {"[B", "OB"}:  # down
                    text = "".join(buffer)
                    if "\n" in text:
                        # multi-line: move cursor down one logical line
                        before = text[:cursor]
                        line_idx = before.count("\n")
                        col = len(before.split("\n")[-1])
                        lines = text.split("\n")
                        if line_idx >= len(lines) - 1:
                            cursor = len(buffer)
                        else:
                            target_col = min(col, len(lines[line_idx + 1]))
                            cursor = sum(len(lines[i]) + 1 for i in range(line_idx + 1)) + target_col
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
        text = "".join(buffer)
        lines = text.split("\n")
        if not lines:
            lines = [""]

        columns = os.get_terminal_size().columns

        # count visual rows per logical line, accounting for terminal wrapping
        visual_per_line: list[int] = []
        for i, line in enumerate(lines):
            prefix_len = len(prompt) if i == 0 else 4  # "... " continuation
            char_count = prefix_len + len(line)
            visual_per_line.append(max(1, -(-char_count // columns)))
        visual_lines = sum(visual_per_line)

        # move from cursor's current visual row to render start, clear, reset
        self._move_up(prev_cursor_row)
        self._clear_n_lines(prev_total)
        self._move_up(prev_total - 1)

        for line_index, line in enumerate(lines):
            if line_index == 0:
                self._write(f"{prompt}{line}")
            else:
                self._write(f"\r\n... {line}")

        # position cursor accounting for wrapping
        cursor_line = text[:cursor].count("\n")
        cursor_col_in_line = len(text[:cursor].split("\n")[-1])
        prefix_len = len(prompt) if cursor_line == 0 else 4
        absolute_col = prefix_len + cursor_col_in_line

        # visual row and column of the cursor within the full render;
        # at exact wrap boundaries (absolute_col % columns == 0) the terminal
        # places the cursor at column 0 of the next row, but our visual_per_line
        # doesn't count that phantom row — keep cursor on the last content row
        cursor_visual_row = sum(visual_per_line[:cursor_line])
        if absolute_col > 0 and absolute_col % columns == 0:
            cursor_visual_row += absolute_col // columns - 1
            cursor_column = columns
        else:
            cursor_visual_row += absolute_col // columns
            cursor_column = absolute_col % columns

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

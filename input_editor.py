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
        previous_display_lines = 1

        self._write(prompt)

        with _raw_terminal_mode(sys.stdin.fileno()):
            decoder = codecs.getincrementaldecoder("utf-8")()
            while True:
                char = os.read(sys.stdin.fileno(), 1)
                if not char:
                    return InputEvent(kind="quit")

                key = _decode_utf8_key(decoder, char)
                if key is None:
                    continue

                if key == "\t":
                    self._clear_render(prompt, buffer, previous_display_lines)
                    return InputEvent(kind="toggle")

                if key == "\x03":
                    raise KeyboardInterrupt

                if key == "\x04":
                    self._write("\r\n")
                    return InputEvent(kind="quit")

                # ctrl+j (0x0a / \n) inserts a newline into the buffer
                if key == "\x0a":
                    buffer.insert(cursor, "\n")
                    cursor += 1
                    previous_display_lines = self._render(prompt, buffer, cursor, previous_display_lines)
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
                    previous_display_lines = self._render(prompt, buffer, cursor, previous_display_lines)
                    continue

                if key == "\x1b":
                    sequence = self._read_escape_sequence()
                    if sequence in {"[C", "OC"}:  # right
                        cursor = min(cursor + 1, len(buffer))
                    elif sequence in {"[D", "OD"}:  # left
                        cursor = max(cursor - 1, 0)
                    elif sequence in {"[A", "OA"}:  # up history
                        if self._history:
                            if history_index is None:
                                history_index = len(self._history) - 1
                            else:
                                history_index = max(0, history_index - 1)
                            buffer = list(self._history[history_index])
                            cursor = len(buffer)
                    elif sequence in {"[B", "OB"}:  # down history
                        if self._history:
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
                    previous_display_lines = self._render(prompt, buffer, cursor, previous_display_lines)
                    continue

                # printable characters
                if key and key >= " ":
                    buffer.insert(cursor, key)
                    cursor += 1
                    previous_display_lines = self._render(prompt, buffer, cursor, previous_display_lines)

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
        previous_display_lines: int,
    ) -> int:
        """Redraw prompt + buffer and reposition the cursor."""
        text = "".join(buffer)
        lines = text.split("\n")
        if not lines:
            lines = [""]
        display_lines = len(lines)

        # move to first line of previous render and clear it
        self._move_to_render_start(previous_display_lines)
        self._clear_n_lines(previous_display_lines)
        self._move_to_render_start(previous_display_lines)

        for line_index, line in enumerate(lines):
            if line_index == 0:
                self._write(f"{prompt}{line}")
            else:
                self._write(f"\n... {line}")

        cursor_line = text[:cursor].count("\n")
        cursor_line_offset = (display_lines - 1) - cursor_line

        if cursor_line_offset > 0:
            self._write(f"\x1b[{cursor_line_offset}A")
        self._write("\r")

        column = len(prompt) + len(text[:cursor].split("\n")[-1])
        if cursor_line > 0:
            column = 4 + len(text[:cursor].split("\n")[-1])
        if column > 0:
            self._write(f"\x1b[{column}C")
        return display_lines

    def _clear_render(self, prompt: str, buffer: list[str], previous_display_lines: int) -> None:
        """Clear the current input render from the terminal."""
        _ = prompt
        _ = buffer
        self._move_to_render_start(previous_display_lines)
        self._clear_n_lines(previous_display_lines)
        self._move_to_render_start(previous_display_lines)

    def _move_to_render_start(self, lines: int) -> None:
        """Move cursor to the first line of the current render."""
        self._write("\r")
        if lines > 1:
            self._write(f"\x1b[{lines - 1}A")

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

"""Tests for InputEditor bracketed paste and basic key handling."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from claodex.input_editor import InputEditor, InputEvent


FAKE_FD = 99


def _feed_bytes(
    raw: bytes,
    *,
    buffer: list[str] | None = None,
    cursor: int = 0,
    history: list[str] | None = None,
) -> InputEvent:
    """Feed a raw byte sequence to InputEditor._read_loop and return the event.

    Mocks os.read to serve one byte at a time from *raw*, select.select to
    always report ready (so escape sequence reads don't time out), and
    _write/_render to suppress terminal output.
    """
    stream = iter(raw)

    def fake_read(_fd, _n):
        try:
            return bytes([next(stream)])
        except StopIteration:
            return b""

    # select always reports ready so escape sequences are consumed greedily
    def fake_select(rlist, _w, _x, _timeout=None):
        return rlist, [], []

    editor = InputEditor()
    if history:
        editor._history = list(history)
    noop_render = lambda *a, **kw: (1, 0)

    if buffer is None:
        buffer = []

    with (
        patch("os.read", side_effect=fake_read),
        patch("select.select", side_effect=fake_select),
        patch("sys.stdin") as mock_stdin,
        patch.object(editor, "_write"),
        patch.object(editor, "_render", side_effect=noop_render),
        patch.object(editor, "_clear_render"),
    ):
        mock_stdin.fileno.return_value = FAKE_FD
        return editor._read_loop(
            prompt="test > ",
            buffer=buffer,
            cursor=cursor,
            history_index=None,
            pasting=False,
            previous_render=(1, 0),
        )


# -- bracketed paste helpers --------------------------------------------------

# wrap content in bracketed paste escape sequences, ending with Enter (\r)
def _paste_then_enter(text: str) -> bytes:
    return b"\x1b[200~" + text.encode() + b"\x1b[201~" + b"\r"


# -- tests --------------------------------------------------------------------


def test_paste_multiline_preserves_newlines():
    """Pasting multi-line text preserves internal newlines as buffer content."""
    event = _feed_bytes(_paste_then_enter("line1\rline2\rline3"))
    assert event.kind == "submit"
    assert event.value == "line1\nline2\nline3"


def test_paste_preserves_tabs():
    """Pasting text with tabs preserves them in the buffer."""
    event = _feed_bytes(_paste_then_enter("col1\tcol2\tcol3"))
    assert event.kind == "submit"
    assert event.value == "col1\tcol2\tcol3"


def test_paste_preserves_tabs_and_newlines():
    """Tabs and newlines in the same paste both survive."""
    event = _feed_bytes(_paste_then_enter("a\tb\rc\td"))
    assert event.kind == "submit"
    assert event.value == "a\tb\nc\td"


def test_paste_ctrl_c_suppressed():
    """Ctrl+C inside paste does not raise KeyboardInterrupt."""
    event = _feed_bytes(_paste_then_enter("before\x03after"))
    assert event.kind == "submit"
    # ctrl+c is not printable (< " ") so it's silently dropped during paste,
    # but crucially it does not raise KeyboardInterrupt
    assert "before" in event.value
    assert "after" in event.value


def test_paste_ctrl_d_suppressed():
    """Ctrl+D inside paste does not trigger quit."""
    event = _feed_bytes(_paste_then_enter("hello\x04world"))
    assert event.kind == "submit"
    assert "hello" in event.value
    assert "world" in event.value


def test_tab_without_paste_toggles():
    """Tab outside paste mode returns a toggle event."""
    event = _feed_bytes(b"\t")
    assert event.kind == "toggle"


def test_enter_submits_normally():
    """Enter without paste submits the buffer."""
    event = _feed_bytes(b"hello\r")
    assert event.kind == "submit"
    assert event.value == "hello"


def test_plain_multiline_submits_first_line():
    """Without paste brackets, \\r submits immediately (first line only)."""
    event = _feed_bytes(b"first\rsecond\r")
    assert event.kind == "submit"
    assert event.value == "first"


def test_ctrl_j_inserts_newline_outside_paste():
    """Ctrl+J (\\n) inserts a newline even outside paste mode."""
    event = _feed_bytes(b"a\nb\r")
    assert event.kind == "submit"
    assert event.value == "a\nb"


def test_empty_paste_submits_empty():
    """Empty paste followed by Enter submits empty string."""
    event = _feed_bytes(b"\x1b[200~\x1b[201~\r")
    assert event.kind == "submit"
    assert event.value == ""


# -- delete (forward) key ----------------------------------------------------


def test_delete_key_removes_char_at_cursor():
    """Delete key ([3~) removes the character under the cursor."""
    # buffer: "ab|cd" with cursor at position 2 → should delete 'c'
    buf = list("abcd")
    event = _feed_bytes(b"\x1b[3~\r", buffer=buf, cursor=2)
    assert event.kind == "submit"
    assert event.value == "abd"


def test_delete_key_at_end_is_noop():
    """Delete key at end of buffer does nothing."""
    buf = list("abc")
    event = _feed_bytes(b"\x1b[3~\r", buffer=buf, cursor=3)
    assert event.kind == "submit"
    assert event.value == "abc"


# -- up/down row navigation in multi-line buffers ----------------------------

# ESC [ A = up, ESC [ B = down

def test_up_moves_to_previous_line():
    """Up arrow in multi-line buffer moves cursor to the previous line."""
    # buffer: "abc\ndef" cursor at 5 (second line, col 1 → 'd')
    # up should move to col 1 on first line → 'b', cursor=1
    # then submit to verify cursor position via what gets typed next
    buf = list("abc\ndef")
    # up then type 'X' then submit — result should be "aXbc\ndef"
    event = _feed_bytes(b"\x1b[AX\r", buffer=buf, cursor=5)
    assert event.kind == "submit"
    assert event.value == "aXbc\ndef"


def test_up_on_first_line_goes_home():
    """Up arrow on the first line of multi-line buffer moves cursor to 0."""
    buf = list("abc\ndef")
    # cursor at 2 (first line, col 2), up → home (0), type 'X' → "Xabc\ndef"
    event = _feed_bytes(b"\x1b[AX\r", buffer=buf, cursor=2)
    assert event.kind == "submit"
    assert event.value == "Xabc\ndef"


def test_down_moves_to_next_line():
    """Down arrow in multi-line buffer moves cursor to the next line."""
    # buffer: "abc\ndef" cursor at 1 (first line, col 1)
    # down → col 1 on second line = position 5 ('e'), type 'X' → "abc\ndXef"
    buf = list("abc\ndef")
    event = _feed_bytes(b"\x1b[BX\r", buffer=buf, cursor=1)
    assert event.kind == "submit"
    assert event.value == "abc\ndXef"


def test_down_on_last_line_goes_end():
    """Down arrow on the last line of multi-line buffer moves cursor to end."""
    buf = list("abc\ndef")
    # cursor at 5 (second line, col 1), down → end (7), type 'X' → "abc\ndefX"
    event = _feed_bytes(b"\x1b[BX\r", buffer=buf, cursor=5)
    assert event.kind == "submit"
    assert event.value == "abc\ndefX"


def test_up_clamps_to_shorter_line():
    """Up arrow clamps column when previous line is shorter."""
    # buffer: "ab\ncdef" cursor at 7 (second line, col 3 past end of 'ab')
    # up → clamp to col 2 on first line (end of "ab"), type 'X' → "abX\ncdef"
    buf = list("ab\ncdef")
    event = _feed_bytes(b"\x1b[AX\r", buffer=buf, cursor=7)
    assert event.kind == "submit"
    assert event.value == "abX\ncdef"


def test_up_single_line_uses_history():
    """Up arrow on single-line buffer navigates history as before."""
    event = _feed_bytes(b"\x1b[A\r", history=["previous"])
    assert event.kind == "submit"
    assert event.value == "previous"


def test_down_single_line_uses_history():
    """Down arrow restores empty buffer after cycling through history."""
    # start with history, up to load it, then down to clear
    event = _feed_bytes(b"\x1b[A\x1b[B\r", history=["prev"])
    assert event.kind == "submit"
    assert event.value == ""


def test_render_continuation_uses_crlf_prefix():
    """Multi-line render uses CRLF before continuation prefix."""
    editor = InputEditor()
    writes: list[str] = []

    with (
        patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
        patch.object(editor, "_move_up"),
        patch.object(editor, "_clear_n_lines"),
        patch.object(editor, "_write", side_effect=writes.append),
    ):
        editor._render(
            prompt="test > ",
            buffer=list("line1\nline2"),
            cursor=len("line1\nline2"),
            previous_render=(1, 0),
        )

    assert "\r\n... line2" in "".join(writes)

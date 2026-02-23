"""Tests for InputEditor bracketed paste and basic key handling."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from claodex.input_editor import InputEditor, InputEvent


FAKE_FD = 99


def _feed_bytes(raw: bytes) -> InputEvent:
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
    noop_render = lambda *a, **kw: (1, 0)

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
            buffer=[],
            cursor=0,
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

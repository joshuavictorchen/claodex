"""Tests for InputEditor bracketed paste and basic key handling."""

from __future__ import annotations

from contextlib import nullcontext
import os
from unittest.mock import patch

import pytest

from claodex.input_editor import InputEditor, InputEvent, _colored_prompt, _visible_len


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
        # history entries are (prompt, text) tuples; tests pass bare strings
        # for convenience so wrap them with a dummy prompt
        editor._history = [("test > ", h) for h in history]
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


def _feed_read(
    raw: bytes,
    *,
    prefill: str = "",
    on_idle=None,
) -> InputEvent:
    """Feed raw bytes through InputEditor.read and return the event."""
    stream = iter(raw)

    def fake_read(_fd, _n):
        try:
            return bytes([next(stream)])
        except StopIteration:
            return b""

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
        patch("claodex.input_editor._raw_terminal_mode", side_effect=lambda _fd: nullcontext()),
    ):
        mock_stdin.fileno.return_value = FAKE_FD
        return editor.read("test", on_idle=on_idle, prefill=prefill, idle_interval=0.01)


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


def test_tab_toggle_preserves_buffer_text():
    """Tab toggle carries in-progress buffer text in event value."""
    event = _feed_bytes(b"draft message\t")
    assert event.kind == "toggle"
    assert event.value == "draft message"


def test_enter_submits_normally():
    """Enter without paste submits the buffer."""
    event = _feed_bytes(b"hello\r")
    assert event.kind == "submit"
    assert event.value == "hello"


def test_enter_writes_separator_between_input_blocks():
    """Enter writes a dim horizontal separator before the next prompt."""
    stream = iter(b"hello\r")
    writes: list[str] = []

    def fake_read(_fd, _n):
        try:
            return bytes([next(stream)])
        except StopIteration:
            return b""

    def fake_select(rlist, _w, _x, _timeout=None):
        return rlist, [], []

    editor = InputEditor()
    with (
        patch("os.read", side_effect=fake_read),
        patch("select.select", side_effect=fake_select),
        patch("os.get_terminal_size", return_value=os.terminal_size((12, 24))),
        patch("sys.stdin") as mock_stdin,
        patch.object(editor, "_render", return_value=(1, 0)),
        patch.object(editor, "_clear_render"),
        patch.object(editor, "_write", side_effect=writes.append),
    ):
        mock_stdin.fileno.return_value = FAKE_FD
        event = editor._read_loop(
            prompt="test > ",
            buffer=[],
            cursor=0,
            history_index=None,
            pasting=False,
            previous_render=(1, 0),
        )

    assert event.kind == "submit"
    assert event.value == "hello"
    separator = "\r\x1b[2K\033[90m" + ("─" * 11) + "\033[0m\r\n"
    assert separator in "".join(writes)


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


def test_up_on_first_visual_row_goes_home_for_multiline():
    """Up arrow on top visual row now jumps to buffer start."""
    buf = list("abc\ndef")
    # cursor at 2 (first visual row), up -> home (0), then insert
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


def test_down_on_last_visual_row_goes_end_for_multiline():
    """Down arrow on bottom visual row now jumps to buffer end."""
    buf = list("abc\ndef")
    # cursor at 6 (last visual row), down -> end (7), then insert
    event = _feed_bytes(b"\x1b[BX\r", buffer=buf, cursor=6)
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


def test_up_single_visual_row_non_empty_uses_history():
    """History navigation still applies for non-empty single visual-row input."""
    event = _feed_bytes(
        b"\x1b[A\r",
        buffer=list("draft"),
        cursor=5,
        history=["previous"],
    )
    assert event.kind == "submit"
    assert event.value == "previous"


def test_down_single_line_uses_history():
    """Down arrow restores empty buffer after cycling through history."""
    # start with history, up to load it, then down to clear
    event = _feed_bytes(b"\x1b[A\x1b[B\r", history=["prev"])
    assert event.kind == "submit"
    assert event.value == ""


def test_down_after_history_restores_single_line_draft():
    """Up enters history and down restores the in-progress single-line draft."""
    event = _feed_bytes(
        b"\x1b[A\x1b[B\r",
        buffer=list("draft"),
        cursor=5,
        history=["prev"],
    )
    assert event.kind == "submit"
    assert event.value == "draft"


def test_up_moves_to_previous_wrapped_visual_row():
    """Up arrow moves across soft-wrapped visual rows (not just newlines)."""
    buf = list("abcdefghij")
    with patch("os.get_terminal_size", return_value=os.terminal_size((12, 24))):
        event = _feed_bytes(b"\x1b[AX\r", buffer=buf, cursor=7)
    assert event.kind == "submit"
    assert event.value == "abXcdefghij"


def test_down_moves_to_next_wrapped_visual_row():
    """Down arrow moves across soft-wrapped visual rows (not just newlines)."""
    buf = list("abcdefghij")
    with patch("os.get_terminal_size", return_value=os.terminal_size((12, 24))):
        event = _feed_bytes(b"\x1b[BX\r", buffer=buf, cursor=2)
    assert event.kind == "submit"
    assert event.value == "abcdefgXhij"


def test_wrapped_visual_rows_take_precedence_over_history():
    """When input wraps to multiple visual rows, up/down move cursor not history."""
    buf = list("abcdefghij")
    with patch("os.get_terminal_size", return_value=os.terminal_size((12, 24))):
        event = _feed_bytes(
            b"\x1b[AX\r",
            buffer=buf,
            cursor=7,
            history=["previous"],
        )
    assert event.kind == "submit"
    assert event.value == "abXcdefghij"


def test_up_on_first_wrapped_visual_row_goes_home():
    """Up at the top wrapped row jumps to buffer start for single-line input."""
    buf = list("abcdefghij")
    with patch("os.get_terminal_size", return_value=os.terminal_size((12, 24))):
        event = _feed_bytes(b"\x1b[AX\r", buffer=buf, cursor=2)
    assert event.kind == "submit"
    assert event.value == "Xabcdefghij"


def test_down_on_last_wrapped_visual_row_goes_end():
    """Down at the bottom wrapped row jumps to buffer end for single-line input."""
    buf = list("abcdefghij")
    with patch("os.get_terminal_size", return_value=os.terminal_size((12, 24))):
        event = _feed_bytes(b"\x1b[BX\r", buffer=buf, cursor=7)
    assert event.kind == "submit"
    assert event.value == "abcdefghijX"


def test_up_moves_across_word_wrapped_rows():
    """Up/down navigation tracks variable word-wrap segment lengths."""
    buf = list("alpha beta gamma")
    with patch("os.get_terminal_size", return_value=os.terminal_size((16, 24))):
        event = _feed_bytes(b"\x1b[AX\r", buffer=buf, cursor=13)
    assert event.kind == "submit"
    assert event.value == "alpha beXta gamma"


def test_render_wrap_prefers_space_boundary_over_hard_split():
    """Soft wrap should break at spaces when one exists in row width."""
    editor = InputEditor()
    writes: list[str] = []

    with (
        patch("os.get_terminal_size", return_value=os.terminal_size((16, 24))),
        patch.object(editor, "_move_up"),
        patch.object(editor, "_clear_n_lines"),
        patch.object(editor, "_write", side_effect=writes.append),
    ):
        editor._render(
            prompt="test > ",
            buffer=list("alpha beta gamma"),
            cursor=len("alpha beta gamma"),
            previous_render=(1, 0),
        )

    rendered = "".join(writes)
    assert "test > alpha \r\n       beta \r\n       gamma" in rendered


def test_render_continuation_indents_to_prompt_width():
    """Multi-line render aligns continuation lines under message text."""
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

    assert "\r\n       line2" in "".join(writes)


def test_colored_prompt_uses_agent_colors():
    assert _colored_prompt("claude") == "\033[38;5;216mclaude ❯ \033[0m"
    assert _colored_prompt("codex") == "\033[38;5;116m codex ❯ \033[0m"
    assert _colored_prompt("collab") == "\033[90mcollab ❯ \033[0m"


def test_visible_len_ignores_ansi_escape_sequences():
    prompt = _colored_prompt("claude")
    assert _visible_len(prompt) == len("claude ❯ ")


def test_visible_len_handles_empty_and_plain_strings():
    assert _visible_len("") == 0
    assert _visible_len("plain text") == len("plain text")


def test_visible_len_handles_multiple_ansi_sequences():
    value = "\033[31mred\033[0m and \033[94mblue\033[0m"
    assert _visible_len(value) == len("red and blue")


def test_colored_prompt_visible_width_matches_rendered_prompt():
    claude_width = _visible_len(_colored_prompt("claude"))
    codex_width = _visible_len(_colored_prompt("codex"))
    assert claude_width == len("claude ❯ ")
    assert codex_width == claude_width


def test_render_continuation_uses_visible_prompt_width():
    editor = InputEditor()
    writes: list[str] = []

    with (
        patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
        patch.object(editor, "_move_up"),
        patch.object(editor, "_clear_n_lines"),
        patch.object(editor, "_write", side_effect=writes.append),
    ):
        editor._render(
            prompt="\033[38;5;116m codex ❯ \033[0m",
            buffer=list("line1\nline2"),
            cursor=len("line1\nline2"),
            previous_render=(1, 0),
        )

    assert "\r\n         line2" in "".join(writes)


def test_render_wrap_inserts_continuation_prefix_on_overflow():
    """Overflow wraps onto continuation lines with prompt-width indentation."""
    editor = InputEditor()
    writes: list[str] = []

    with (
        patch("os.get_terminal_size", return_value=os.terminal_size((16, 24))),
        patch.object(editor, "_move_up"),
        patch.object(editor, "_clear_n_lines"),
        patch.object(editor, "_write", side_effect=writes.append),
    ):
        render_state = editor._render(
            prompt="test > ",
            buffer=list("abcdefghijk"),
            cursor=len("abcdefghijk"),
            previous_render=(1, 0),
        )

    assert "test > abcdefghi\r\n       jk" in "".join(writes)
    assert render_state == (2, 1)


def test_render_exact_wrap_boundary_keeps_cursor_on_last_content_row():
    """Cursor at end of an exact-width segment stays on the occupied row."""
    editor = InputEditor()
    writes: list[str] = []

    with (
        patch("os.get_terminal_size", return_value=os.terminal_size((16, 24))),
        patch.object(editor, "_move_up"),
        patch.object(editor, "_clear_n_lines"),
        patch.object(editor, "_write", side_effect=writes.append),
    ):
        render_state = editor._render(
            prompt="test > ",
            buffer=list("abcdefghi"),
            cursor=len("abcdefghi"),
            previous_render=(1, 0),
        )

    rendered = "".join(writes)
    assert "test > abcdefghi" in rendered
    assert "\r\n       " not in rendered
    assert render_state == (1, 0)


def test_read_prefill_submits_existing_text():
    """Prefill text is submitted when Enter is pressed immediately."""
    event = _feed_read(b"\r", prefill="draft")
    assert event.kind == "submit"
    assert event.value == "draft"


def test_idle_callback_stashes_in_progress_buffer():
    """Idle callback returns event with draft text preserved in value."""
    editor = InputEditor()
    called = {"count": 0}

    def on_idle():
        called["count"] += 1
        return InputEvent(kind="collab_initiated")

    def fake_select(_r, _w, _x, _timeout=None):
        return [], [], []

    with (
        patch("select.select", side_effect=fake_select),
        patch("sys.stdin") as mock_stdin,
        patch.object(editor, "_write"),
        patch.object(editor, "_clear_render"),
        patch.object(editor, "_render", return_value=(1, 0)),
    ):
        mock_stdin.fileno.return_value = FAKE_FD
        event = editor._read_loop(
            prompt="test > ",
            buffer=list("draft"),
            cursor=5,
            history_index=None,
            pasting=False,
            previous_render=(1, 0),
            on_idle=on_idle,
            idle_interval=0.01,
        )

    assert called["count"] == 1
    assert event.kind == "collab_initiated"
    assert event.value == "draft"


def test_idle_callback_suppressed_while_bracketed_paste_active():
    """Idle callback must not fire while inside bracketed paste."""
    editor = InputEditor()
    idle_calls = {"count": 0}
    select_calls = {"count": 0}
    stream = iter(b"\x1b[201~\r")

    def on_idle():
        idle_calls["count"] += 1
        return InputEvent(kind="collab_initiated")

    def fake_select(rlist, _w, _x, _timeout=None):
        select_calls["count"] += 1
        if select_calls["count"] == 1:
            return [], [], []
        return rlist, [], []

    def fake_read(_fd, _n):
        try:
            return bytes([next(stream)])
        except StopIteration:
            return b""

    with (
        patch("select.select", side_effect=fake_select),
        patch("os.read", side_effect=fake_read),
        patch("sys.stdin") as mock_stdin,
        patch.object(editor, "_write"),
        patch.object(editor, "_clear_render"),
        patch.object(editor, "_render", return_value=(1, 0)),
    ):
        mock_stdin.fileno.return_value = FAKE_FD
        event = editor._read_loop(
            prompt="test > ",
            buffer=list("partial"),
            cursor=7,
            history_index=None,
            pasting=True,
            previous_render=(1, 0),
            on_idle=on_idle,
            idle_interval=0.01,
        )

    assert idle_calls["count"] == 0
    assert event.kind == "submit"
    assert event.value == "partial"


def test_read_loop_rerenders_when_terminal_width_changes():
    """Loop polls terminal width and re-renders immediately on resize."""
    editor = InputEditor()
    stream = iter(b"\r")

    def fake_read(_fd, _n):
        try:
            return bytes([next(stream)])
        except StopIteration:
            return b""

    def fake_select(rlist, _w, _x, _timeout=None):
        return rlist, [], []

    with (
        patch("select.select", side_effect=fake_select),
        patch("os.read", side_effect=fake_read),
        patch("claodex.input_editor._terminal_columns", side_effect=[20, 30, 30]),
        patch("sys.stdin") as mock_stdin,
        patch.object(editor, "_write") as write_mock,
        patch.object(editor, "_clear_render"),
        patch.object(editor, "_render", return_value=(1, 0)) as render_mock,
    ):
        mock_stdin.fileno.return_value = FAKE_FD
        event = editor._read_loop(
            prompt="test > ",
            buffer=list("abc"),
            cursor=3,
            history_index=None,
            pasting=False,
            previous_render=(1, 0),
        )

    assert event.kind == "submit"
    assert "\033[2J\033[H\033[3J" in "".join(call.args[0] for call in write_mock.call_args_list)
    render_mock.assert_called_once_with("test > ", ["a", "b", "c"], 3, (1, 0))


def test_read_loop_replays_recent_history_on_resize():
    """Resize path clears and redraws recent submitted history entries."""
    editor = InputEditor()
    editor._history = [("test > ", "earlier message")]
    stream = iter(b"\r")

    def fake_read(_fd, _n):
        try:
            return bytes([next(stream)])
        except StopIteration:
            return b""

    def fake_select(rlist, _w, _x, _timeout=None):
        return rlist, [], []

    with (
        patch("select.select", side_effect=fake_select),
        patch("os.read", side_effect=fake_read),
        patch("claodex.input_editor._terminal_columns", side_effect=[20, 30, 30, 30, 30]),
        patch("sys.stdin") as mock_stdin,
        patch.object(editor, "_write") as write_mock,
        patch.object(editor, "_clear_render"),
        patch.object(editor, "_render", return_value=(1, 0)),
    ):
        mock_stdin.fileno.return_value = FAKE_FD
        event = editor._read_loop(
            prompt="test > ",
            buffer=list("abc"),
            cursor=3,
            history_index=None,
            pasting=False,
            previous_render=(1, 0),
        )

    rendered = "".join(call.args[0] for call in write_mock.call_args_list)
    assert event.kind == "submit"
    assert "test > earlier message" in rendered
    assert "\033[2J\033[H\033[3J" in rendered


def test_resize_replay_preserves_per_entry_prompts():
    """Resize replay renders each history entry with its original prompt."""
    claude_prompt = _colored_prompt("claude")
    collab_prompt = _colored_prompt("collab")
    editor = InputEditor()
    editor._history = [
        (claude_prompt, "hello world"),
        (collab_prompt, "collab msg"),
    ]
    stream = iter(b"\r")

    def fake_read(_fd, _n):
        try:
            return bytes([next(stream)])
        except StopIteration:
            return b""

    def fake_select(rlist, _w, _x, _timeout=None):
        return rlist, [], []

    with (
        patch("select.select", side_effect=fake_select),
        patch("os.read", side_effect=fake_read),
        # first call returns initial width, second returns different width to
        # trigger resize, remaining calls service render/layout
        patch("claodex.input_editor._terminal_columns", side_effect=[20, 30, 30, 30, 30, 30, 30]),
        patch("sys.stdin") as mock_stdin,
        patch.object(editor, "_write") as write_mock,
        patch.object(editor, "_clear_render"),
        patch.object(editor, "_render", return_value=(1, 0)),
    ):
        mock_stdin.fileno.return_value = FAKE_FD
        editor._read_loop(
            prompt=collab_prompt,
            buffer=list("x"),
            cursor=1,
            history_index=None,
            pasting=False,
            previous_render=(1, 0),
        )

    rendered = "".join(call.args[0] for call in write_mock.call_args_list)
    # the claude entry must render with the claude prompt, not the current
    # collab prompt — this is the exact bug that was fixed
    assert f"{claude_prompt}hello world" in rendered
    assert f"{collab_prompt}collab msg" in rendered

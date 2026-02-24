from __future__ import annotations

import curses
import json
from datetime import datetime, timezone
from unittest.mock import patch

from claodex.sidebar import (
    LogEntry,
    SidebarApplication,
    _active_thinking_agent,
    _as_text,
    _collect_capped_output,
    _default_metrics_snapshot,
    _derive_completed_thinking_seconds,
    _derive_inflight_thinking_seconds,
    _derive_turn_counts,
    _format_elapsed,
    _load_metrics_snapshot,
    _looks_interactive_command,
    _mode_text,
    _parse_iso8601,
    _parse_event_line,
    _status_text,
    _uptime_text,
)


def test_parse_event_line_parses_valid_jsonl_row():
    raw = json.dumps(
        {
            "ts": "2026-02-24T01:30:00+00:00",
            "kind": "sent",
            "agent": None,
            "target": "claude",
            "message": "-> claude",
            "meta": None,
        }
    )
    entry = _parse_event_line(raw)
    assert entry is not None
    assert entry.kind == "sent"
    assert entry.target == "claude"
    assert entry.timestamp.isoformat() == "2026-02-24T01:30:00+00:00"


def test_parse_event_line_rejects_invalid_rows():
    assert _parse_event_line("{") is None
    assert _parse_event_line(json.dumps({"ts": "2026-02-24T01:30:00+00:00"})) is None


def test_load_metrics_snapshot_merges_known_fields(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text(
        json.dumps(
            {
                "mode": "collab",
                "collab_max": 6,
                "unknown": "ignored",
                "agents": {
                    "claude": {
                        "status": "thinking",
                        "last_words": 120,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    current = _default_metrics_snapshot()
    merged = _load_metrics_snapshot(path, current)
    assert merged["mode"] == "collab"
    assert merged["collab_max"] == 6
    assert merged["agents"]["claude"]["status"] == "thinking"
    assert merged["agents"]["claude"]["last_words"] == 120
    assert merged["agents"]["codex"]["status"] == "idle"
    assert "unknown" not in merged


def test_load_metrics_snapshot_tolerates_missing_or_invalid_file(tmp_path):
    missing = tmp_path / "missing.json"
    current = _default_metrics_snapshot()
    assert _load_metrics_snapshot(missing, current) == current

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert _load_metrics_snapshot(broken, current) == current


def test_collect_capped_output_limits_lines_and_bytes():
    lines, truncated = _collect_capped_output(
        stdout="one\ntwo\nthree\nfour\n",
        stderr="",
        max_lines=3,
        max_bytes=10_000,
    )
    assert lines == ["one", "two", "three"]
    assert truncated is True

    lines, truncated = _collect_capped_output(
        stdout="abcdef\n",
        stderr="",
        max_lines=10,
        max_bytes=4,
    )
    assert lines == []
    assert truncated is True


def test_looks_interactive_command_detects_known_interactive_tools():
    assert _looks_interactive_command("vim notes.txt") is True
    assert _looks_interactive_command("echo hello") is False


def test_mode_and_uptime_text_format_collab_state():
    metrics = _default_metrics_snapshot()
    metrics["mode"] = "collab"
    metrics["collab_turn"] = 2
    metrics["collab_max"] = 8
    metrics["uptime_start"] = "2026-02-24T01:00:00+00:00"

    now = datetime(2026, 2, 24, 1, 30, 0, tzinfo=timezone.utc)
    assert _mode_text(metrics) == "collaborative 2/8"
    assert _uptime_text(metrics, now=now) == "30m00s"

    metrics["collab_turn"] = None
    assert _mode_text(metrics) == "collaborative ?/8"


def test_status_text_and_active_thinking_agent():
    metrics = _default_metrics_snapshot()
    assert _active_thinking_agent(metrics) is None
    assert _status_text(metrics, spinner_frame="|") == (". idle", None)

    metrics["agents"]["claude"]["status"] = "thinking"
    metrics["agents"]["claude"]["thinking_since"] = "2026-02-24T01:30:00+00:00"
    assert _active_thinking_agent(metrics) == "claude"
    assert _status_text(metrics, spinner_frame="|") == ("| claude", "claude")

    metrics["agents"]["codex"]["status"] = "thinking"
    metrics["agents"]["codex"]["thinking_since"] = "2026-02-24T01:30:10+00:00"
    assert _active_thinking_agent(metrics) == "codex"


def test_derive_turn_counts_and_completed_thinking_seconds():
    base = datetime(2026, 2, 24, 1, 30, 0, tzinfo=timezone.utc)
    entries = [
        LogEntry(timestamp=base, kind="sent", message="-> claude", target="claude"),
        LogEntry(timestamp=base.replace(second=5), kind="recv", message="<- claude", agent="claude"),
        LogEntry(timestamp=base.replace(second=10), kind="sent", message="-> codex", target="codex"),
        LogEntry(timestamp=base.replace(second=14), kind="recv", message="<- codex", agent="codex"),
        LogEntry(timestamp=base.replace(second=20), kind="recv", message="<- codex", agent="codex"),
    ]
    assert _derive_turn_counts(entries) == {"claude": 1, "codex": 2}
    assert _derive_completed_thinking_seconds(entries) == 9.0


def test_derive_inflight_thinking_seconds_uses_active_thinking_since():
    metrics = _default_metrics_snapshot()
    metrics["agents"]["claude"]["status"] = "thinking"
    metrics["agents"]["claude"]["thinking_since"] = "2026-02-24T01:29:48+00:00"
    metrics["agents"]["codex"]["status"] = "idle"
    metrics["agents"]["codex"]["thinking_since"] = "2026-02-24T01:29:00+00:00"

    now = datetime(2026, 2, 24, 1, 30, 0, tzinfo=timezone.utc)
    assert _derive_inflight_thinking_seconds(metrics, now=now) == 12.0


def test_format_elapsed_handles_boundary_and_large_values():
    assert _format_elapsed(0) == "0s"
    assert _format_elapsed(3600) == "1h00m"
    assert _format_elapsed(36610) == "10h10m"


def test_as_text_handles_none_bytes_and_string():
    assert _as_text(None) == ""
    assert _as_text(b"hello") == "hello"
    assert _as_text("already text") == "already text"


def test_parse_iso8601_accepts_timezone_and_rejects_invalid_values():
    assert _parse_iso8601("2026-02-24T01:30:00+00:00") is not None
    assert _parse_iso8601("not-a-timestamp") is None
    assert _parse_iso8601("2026-02-24T01:30:00") is None


def test_read_event_lines_handles_partial_fragment_reassembly(tmp_path):
    workspace = tmp_path / "workspace"
    app = SidebarApplication(workspace)
    app._events_path.parent.mkdir(parents=True, exist_ok=True)

    app._events_path.write_text("one\ntwo", encoding="utf-8")
    assert app._read_event_lines() == ["one"]

    with app._events_path.open("a", encoding="utf-8") as handle:
        handle.write("\nthree\n")
    assert app._read_event_lines() == ["two", "three"]


def test_wrapped_log_lines_aligns_kind_and_continuation(tmp_path):
    workspace = tmp_path / "workspace"
    app = SidebarApplication(workspace)
    entry = LogEntry(
        timestamp=datetime(2026, 2, 24, 1, 30, 0, tzinfo=timezone.utc),
        kind="sent",
        message="alpha beta gamma delta",
    )
    app._entries.append(entry)

    wrapped = app._wrapped_log_lines(width=24)
    prefix = f"{entry.timestamp.astimezone().strftime('%H:%M:%S')}   [sent] "

    assert wrapped[0][0].startswith(prefix)
    assert len(wrapped) > 1
    assert wrapped[1][0].startswith(" " * len(prefix))


def test_handle_input_key_page_scroll_adjusts_offset(tmp_path):
    workspace = tmp_path / "workspace"
    app = SidebarApplication(workspace)
    app._last_log_height = 4

    app._handle_input_key(curses.KEY_PPAGE)
    assert app._scroll_offset == 3
    app._handle_input_key(curses.KEY_PPAGE)
    assert app._scroll_offset == 6

    app._handle_input_key(curses.KEY_NPAGE)
    assert app._scroll_offset == 3
    app._handle_input_key(curses.KEY_NPAGE)
    assert app._scroll_offset == 0
    app._handle_input_key(curses.KEY_NPAGE)
    assert app._scroll_offset == 0


def test_render_metrics_strip_drops_optional_fields_on_narrow_width(tmp_path):
    class _StdScr:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, str]] = []

        def addnstr(self, row: int, column: int, text: str, _n: int, _attr: int) -> None:
            self.calls.append((row, column, text))

    def _line_text(calls: list[tuple[int, int, str]]) -> str:
        return "".join(text for _row, _column, text in sorted(calls, key=lambda item: item[1]))

    workspace = tmp_path / "workspace"
    app = SidebarApplication(workspace)
    app._metrics["mode"] = "collab"
    app._metrics["collab_turn"] = 2
    app._metrics["collab_max"] = 8
    app._metrics["agents"]["codex"]["status"] = "thinking"
    app._metrics["agents"]["codex"]["thinking_since"] = "2026-02-24T01:29:55+00:00"
    app._entries.extend(
        [
            LogEntry(
                timestamp=datetime(2026, 2, 24, 1, 29, 50, tzinfo=timezone.utc),
                kind="recv",
                message="<- claude",
                agent="claude",
            ),
            LogEntry(
                timestamp=datetime(2026, 2, 24, 1, 29, 51, tzinfo=timezone.utc),
                kind="recv",
                message="<- codex",
                agent="codex",
            ),
        ]
    )
    now = datetime(2026, 2, 24, 1, 30, 0, tzinfo=timezone.utc)

    wide = _StdScr()
    app._render_metrics_strip(wide, row=0, width=120, now=now)
    wide_line = _line_text(wide.calls)
    assert "collaborative 2/8" in wide_line
    assert "think " in wide_line
    assert "up " in wide_line
    assert "claude:1" in wide_line
    assert "codex:1" in wide_line

    narrow = _StdScr()
    app._render_metrics_strip(narrow, row=0, width=35, now=now)
    narrow_line = _line_text(narrow.calls)
    assert "collaborative 2/8" in narrow_line
    assert "think " not in narrow_line
    assert "up " not in narrow_line
    assert "claude:" not in narrow_line
    assert "codex:" not in narrow_line


def test_render_metrics_strip_spinner_uses_ascii_frames(tmp_path):
    class _StdScr:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, str]] = []

        def addnstr(self, row: int, column: int, text: str, _n: int, _attr: int) -> None:
            self.calls.append((row, column, text))

    def _line_text(calls: list[tuple[int, int, str]]) -> str:
        return "".join(text for _row, _column, text in sorted(calls, key=lambda item: item[1]))

    workspace = tmp_path / "workspace"
    app = SidebarApplication(workspace)
    app._metrics["agents"]["claude"]["status"] = "thinking"
    app._metrics["agents"]["claude"]["thinking_since"] = "2026-02-24T01:29:55+00:00"

    now = datetime(2026, 2, 24, 1, 30, 0, tzinfo=timezone.utc)
    frame_one = _StdScr()
    app._render_metrics_strip(frame_one, row=0, width=80, now=now)
    frame_two = _StdScr()
    app._render_metrics_strip(frame_two, row=0, width=80, now=now)

    assert _line_text(frame_one.calls).startswith("⠋ claude")
    assert _line_text(frame_two.calls).startswith("⠙ claude")


def test_render_log_applies_scroll_offset_and_clamps(tmp_path):
    workspace = tmp_path / "workspace"
    app = SidebarApplication(workspace)
    wrapped = [(f"line-{index}", 0) for index in range(1, 7)]
    drawn: list[str] = []

    def _capture_line(_stdscr, _row, text, _width, _attr):  # noqa: ANN001
        drawn.append(text)

    with (
        patch.object(app, "_wrapped_log_lines", return_value=wrapped),
        patch.object(app, "_draw_line", side_effect=_capture_line),
        patch.object(app, "_draw_scrollbar"),
    ):
        app._render_log(stdscr=object(), top=0, height=3, width=80)
    assert drawn == ["line-4", "line-5", "line-6"]

    drawn.clear()
    app._scroll_offset = 2
    with (
        patch.object(app, "_wrapped_log_lines", return_value=wrapped),
        patch.object(app, "_draw_line", side_effect=_capture_line),
        patch.object(app, "_draw_scrollbar"),
    ):
        app._render_log(stdscr=object(), top=0, height=3, width=80)
    assert drawn == ["line-2", "line-3", "line-4"]

    drawn.clear()
    app._scroll_offset = 99
    with (
        patch.object(app, "_wrapped_log_lines", return_value=wrapped),
        patch.object(app, "_draw_line", side_effect=_capture_line),
        patch.object(app, "_draw_scrollbar"),
    ):
        app._render_log(stdscr=object(), top=0, height=3, width=80)
    assert app._scroll_offset == 3
    assert drawn == ["line-1", "line-2", "line-3"]


def test_render_log_reserves_right_column_and_draws_scrollbar_on_overflow(tmp_path):
    workspace = tmp_path / "workspace"
    app = SidebarApplication(workspace)
    wrapped = [(f"line-{index}", 0) for index in range(1, 8)]
    drawn_widths: list[int] = []
    stdscr = object()

    def _capture_line(_stdscr, _row, _text, width, _attr):  # noqa: ANN001
        drawn_widths.append(width)

    with (
        patch.object(app, "_wrapped_log_lines", return_value=wrapped) as wrapped_mock,
        patch.object(app, "_draw_line", side_effect=_capture_line),
        patch.object(app, "_draw_scrollbar") as scrollbar_mock,
    ):
        app._render_log(stdscr=stdscr, top=2, height=3, width=20)

    wrapped_mock.assert_called_once_with(19)
    assert drawn_widths == [19, 19, 19]
    scrollbar_mock.assert_called_once()
    assert scrollbar_mock.call_args.args[0] is stdscr
    assert scrollbar_mock.call_args.kwargs == {
        "top": 2,
        "height": 3,
        "column": 19,
        "scroll_offset": 0,
        "max_scroll": 4,
        "total_lines": 7,
    }


def test_render_log_hides_scrollbar_when_content_fits(tmp_path):
    workspace = tmp_path / "workspace"
    app = SidebarApplication(workspace)
    wrapped = [("line-1", 0), ("line-2", 0)]

    with (
        patch.object(app, "_wrapped_log_lines", return_value=wrapped) as wrapped_mock,
        patch.object(app, "_draw_line"),
        patch.object(app, "_draw_scrollbar") as scrollbar_mock,
    ):
        app._render_log(stdscr=object(), top=0, height=3, width=20)

    wrapped_mock.assert_called_once_with(19)
    scrollbar_mock.assert_not_called()


def test_draw_scrollbar_places_thumb_at_bottom_when_at_tail():
    class _StdScr:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int, int]] = []

        def addch(self, row: int, column: int, char: int, attr: int) -> None:
            self.calls.append((row, column, char, attr))

    stdscr = _StdScr()
    with patch("claodex.sidebar.curses.ACS_VLINE", ord("|"), create=True):
        SidebarApplication._draw_scrollbar(
            stdscr,
            top=10,
            height=4,
            column=7,
            scroll_offset=0,
            max_scroll=4,
            total_lines=8,
        )

    bold_rows = [row for row, _column, _char, attr in stdscr.calls if attr == curses.A_BOLD]
    assert bold_rows == [12, 13]


def test_draw_scrollbar_places_thumb_at_top_when_scrolled_to_oldest():
    class _StdScr:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int, int]] = []

        def addch(self, row: int, column: int, char: int, attr: int) -> None:
            self.calls.append((row, column, char, attr))

    stdscr = _StdScr()
    with patch("claodex.sidebar.curses.ACS_VLINE", ord("|"), create=True):
        SidebarApplication._draw_scrollbar(
            stdscr,
            top=10,
            height=4,
            column=7,
            scroll_offset=4,
            max_scroll=4,
            total_lines=8,
        )

    bold_rows = [row for row, _column, _char, attr in stdscr.calls if attr == curses.A_BOLD]
    assert bold_rows == [10, 11]


def test_append_shell_entry_uses_timezone_aware_local_timestamp(tmp_path):
    workspace = tmp_path / "workspace"
    app = SidebarApplication(workspace)
    app._append_shell_entry("echo hello")

    entry = app._entries[-1]
    assert entry.kind == "shell"
    assert entry.timestamp.tzinfo is not None
    assert entry.timestamp.utcoffset() is not None


def test_run_clears_scrollback_before_starting_curses(tmp_path):
    workspace = tmp_path / "workspace"
    app = SidebarApplication(workspace)

    with (
        patch.object(app, "_clear_terminal_scrollback") as clear_mock,
        patch("claodex.sidebar.curses.wrapper", side_effect=KeyboardInterrupt) as wrapper_mock,
    ):
        exit_code = app.run()

    assert exit_code == 0
    clear_mock.assert_called_once_with()
    wrapper_mock.assert_called_once()

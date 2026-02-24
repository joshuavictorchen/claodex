from __future__ import annotations

import curses
import json
from datetime import datetime, timezone
from unittest.mock import patch

from claodex.sidebar import (
    LogEntry,
    SidebarApplication,
    _as_text,
    _collect_capped_output,
    _default_metrics_snapshot,
    _format_elapsed,
    _format_metrics_lines,
    _load_metrics_snapshot,
    _looks_interactive_command,
    _parse_iso8601,
    _parse_event_line,
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


def test_format_metrics_lines_includes_collab_and_uptime():
    metrics = _default_metrics_snapshot()
    metrics["target"] = "codex"
    metrics["mode"] = "collab"
    metrics["collab_turn"] = 2
    metrics["collab_max"] = 8
    metrics["uptime_start"] = "2026-02-24T01:00:00+00:00"
    metrics["agents"]["claude"]["status"] = "thinking"
    metrics["agents"]["claude"]["thinking_since"] = "2026-02-24T01:29:50+00:00"
    metrics["agents"]["claude"]["last_words"] = 320
    metrics["agents"]["claude"]["last_latency_s"] = 2.4
    metrics["agents"]["codex"]["last_words"] = 100
    metrics["agents"]["codex"]["last_latency_s"] = 1.1

    now = datetime(2026, 2, 24, 1, 30, 0, tzinfo=timezone.utc)
    lines = _format_metrics_lines(metrics, now=now)
    assert "target: codex" in lines[0]
    assert "mode: collab 2/8" in lines[0]
    assert "uptime: 30m00s" in lines[0]
    assert "claude: thinking 10s" in lines[1]
    assert "last: claude 320w 2.4s" in lines[2]


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
    assert app._scroll_offset == 4
    app._handle_input_key(curses.KEY_PPAGE)
    assert app._scroll_offset == 8

    app._handle_input_key(curses.KEY_NPAGE)
    assert app._scroll_offset == 4
    app._handle_input_key(curses.KEY_NPAGE)
    assert app._scroll_offset == 0
    app._handle_input_key(curses.KEY_NPAGE)
    assert app._scroll_offset == 0


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

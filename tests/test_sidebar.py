from __future__ import annotations

import json
from datetime import datetime, timezone

from claodex.sidebar import (
    _collect_capped_output,
    _default_metrics_snapshot,
    _format_metrics_lines,
    _load_metrics_snapshot,
    _looks_interactive_command,
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

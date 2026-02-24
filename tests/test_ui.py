from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from claodex.errors import ClaodexError
from claodex.ui import UIEventBus


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _fixed_now() -> datetime:
    return datetime(2026, 2, 24, 1, 30, tzinfo=timezone.utc)


def test_ui_event_bus_initializes_files_with_schema_defaults(tmp_path):
    workspace = tmp_path / "workspace"
    bus = UIEventBus(workspace, now_provider=_fixed_now)
    bus.close()

    events_path = workspace / ".claodex" / "ui" / "events.jsonl"
    metrics_path = workspace / ".claodex" / "ui" / "metrics.json"
    assert events_path.exists()
    assert metrics_path.exists()

    metrics = _read_json(metrics_path)
    assert metrics["target"] == "claude"
    assert metrics["mode"] == "normal"
    assert metrics["collab_turn"] is None
    assert metrics["collab_max"] is None
    assert metrics["uptime_start"] == "2026-02-24T01:30:00+00:00"
    assert metrics["agents"]["claude"]["status"] == "idle"
    assert metrics["agents"]["codex"]["status"] == "idle"


def test_log_appends_event_jsonl(tmp_path):
    workspace = tmp_path / "workspace"
    bus = UIEventBus(workspace, now_provider=_fixed_now)

    bus.log("sent", "routed", target="claude", meta={"turn": 1})
    bus.close()

    events_path = workspace / ".claodex" / "ui" / "events.jsonl"
    rows = events_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1

    event = json.loads(rows[0])
    assert event["ts"] == "2026-02-24T01:30:00+00:00"
    assert event["kind"] == "sent"
    assert event["agent"] is None
    assert event["target"] == "claude"
    assert event["message"] == "routed"
    assert event["meta"] == {"turn": 1}


def test_log_includes_null_optional_fields_when_unset(tmp_path):
    workspace = tmp_path / "workspace"
    bus = UIEventBus(workspace, now_provider=_fixed_now)

    bus.log("system", "ready")
    bus.close()

    events_path = workspace / ".claodex" / "ui" / "events.jsonl"
    rows = events_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1

    event = json.loads(rows[0])
    assert "agent" in event and event["agent"] is None
    assert "target" in event and event["target"] is None
    assert "meta" in event and event["meta"] is None


def test_log_rejects_sidebar_local_shell_kind(tmp_path):
    bus = UIEventBus(tmp_path / "workspace", now_provider=_fixed_now)
    with pytest.raises(ClaodexError, match="unsupported event kind"):
        bus.log("shell", "not persisted")
    bus.close()


def test_update_metrics_merges_partial_fields_and_writes_full_snapshot(tmp_path):
    workspace = tmp_path / "workspace"
    bus = UIEventBus(workspace, now_provider=_fixed_now)
    bus.update_metrics(
        mode="collab",
        collab_turn=2,
        collab_max=8,
        agents={
            "claude": {
                "status": "thinking",
                "thinking_since": "2026-02-24T01:31:00+00:00",
                "last_words": 123,
                "last_latency_s": 1.5,
            }
        },
    )
    bus.close()

    metrics = _read_json(workspace / ".claodex" / "ui" / "metrics.json")
    assert metrics["mode"] == "collab"
    assert metrics["collab_turn"] == 2
    assert metrics["collab_max"] == 8
    assert metrics["agents"]["claude"]["status"] == "thinking"
    assert metrics["agents"]["claude"]["last_words"] == 123
    assert metrics["agents"]["codex"]["status"] == "idle"
    assert metrics["agents"]["codex"]["last_words"] is None


def test_update_metrics_rejects_unknown_field(tmp_path):
    bus = UIEventBus(tmp_path / "workspace", now_provider=_fixed_now)
    with pytest.raises(ClaodexError, match="unknown metrics field"):
        bus.update_metrics(unexpected="value")
    bus.close()


def test_update_metrics_is_thread_safe(tmp_path):
    workspace = tmp_path / "workspace"
    bus = UIEventBus(workspace, now_provider=_fixed_now)

    def writer(turn: int) -> None:
        bus.log("system", f"turn {turn}")
        bus.update_metrics(collab_turn=turn, collab_max=10)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(1, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    bus.close()

    events_path = workspace / ".claodex" / "ui" / "events.jsonl"
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 5

    metrics = _read_json(workspace / ".claodex" / "ui" / "metrics.json")
    assert metrics["collab_turn"] in {1, 2, 3, 4, 5}
    assert metrics["collab_max"] == 10

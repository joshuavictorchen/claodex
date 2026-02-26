from __future__ import annotations

import ast
from datetime import datetime, timezone
import inspect
from pathlib import Path
import threading
from unittest.mock import patch

import pytest

import claodex.cli as cli_module
from claodex.cli import (
    ClaodexApplication,
    CollabRequest,
    _drain_queue,
    _format_local_time,
    _strip_routing_signals,
)
from claodex.errors import ClaodexError
from claodex.input_editor import InputEvent
from claodex.router import PendingSend, ResponseTurn
from claodex.state import (
    Participant,
    SessionParticipants,
    delivery_cursor_file,
    ensure_state_layout,
    participant_file,
    read_cursor,
    read_cursor_file,
    ui_events_file,
    ui_metrics_file,
    write_cursor,
)
from claodex.tmux_ops import PaneLayout


def _build_participants(workspace: Path, session_file: Path) -> SessionParticipants:
    """Build deterministic participant fixtures for REPL tests."""
    return SessionParticipants(
        claude=Participant(
            agent="claude",
            session_file=session_file,
            session_id="claude-session",
            tmux_pane="%1",
            cwd=workspace,
            registered_at="2026-02-23T00:00:00-05:00",
        ),
        codex=Participant(
            agent="codex",
            session_file=session_file,
            session_id="codex-session",
            tmux_pane="%2",
            cwd=workspace,
            registered_at="2026-02-23T00:00:00-05:00",
        ),
    )


class _BusRecorder:
    """Captures UI bus calls for assertions."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.metric_updates: list[dict[str, object]] = []
        self.closed = False

    def log(
        self,
        kind: str,
        message: str,
        *,
        agent: str | None = None,
        target: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> None:
        self.events.append(
            {
                "kind": kind,
                "message": message,
                "agent": agent,
                "target": target,
                "meta": meta,
            }
        )

    def update_metrics(self, **fields: object) -> None:
        self.metric_updates.append(fields)

    def close(self) -> None:
        self.closed = True


def test_clear_session_state_removes_stale_entries(tmp_path):
    """Fresh start must clear leftover participant and cursor files."""
    workspace = tmp_path / "workspace"

    # create all state dirs and stale files using the real path helpers
    for agent in ("claude", "codex"):
        path = participant_file(workspace, agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

        rpath = read_cursor_file(workspace, agent)
        rpath.parent.mkdir(parents=True, exist_ok=True)
        rpath.write_text("100", encoding="utf-8")

        dpath = delivery_cursor_file(workspace, agent)
        dpath.parent.mkdir(parents=True, exist_ok=True)
        dpath.write_text("100", encoding="utf-8")

    events_file = ui_events_file(workspace)
    events_file.parent.mkdir(parents=True, exist_ok=True)
    events_file.write_text("{\"kind\":\"system\"}\n", encoding="utf-8")

    metrics_file = ui_metrics_file(workspace)
    metrics_file.parent.mkdir(parents=True, exist_ok=True)
    metrics_file.write_text("{\"mode\":\"normal\"}\n", encoding="utf-8")

    application = ClaodexApplication()
    application._clear_session_state(workspace)

    for agent in ("claude", "codex"):
        assert not participant_file(workspace, agent).exists()
        assert not read_cursor_file(workspace, agent).exists()
        assert not delivery_cursor_file(workspace, agent).exists()
    assert not events_file.exists()
    assert not metrics_file.exists()


def test_clear_session_state_noop_when_no_files(tmp_path):
    """Clearing is safe when state dirs don't exist yet."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    application = ClaodexApplication()
    # should not raise
    application._clear_session_state(workspace)


def test_load_or_wait_participants_clears_screen_only_after_wait_path(tmp_path):
    """Screen clear runs only on fresh-start wait, not normal reattach load."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("", encoding="utf-8")
    participants = _build_participants(workspace, session_file)

    application = ClaodexApplication()

    # reattach path: participant files already exist, no wait, no clear
    for agent in ("claude", "codex"):
        path = participant_file(workspace, agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    with (
        patch("claodex.cli.load_participants", return_value=participants) as load_mock,
        patch.object(application, "_wait_for_registration") as wait_mock,
        patch.object(application, "_clear_terminal_screen") as clear_mock,
    ):
        result = application._load_or_wait_participants(workspace)

    assert result == participants
    load_mock.assert_called_once_with(workspace)
    wait_mock.assert_not_called()
    clear_mock.assert_not_called()

    # fresh-start path: participant files missing, wait then clear
    for agent in ("claude", "codex"):
        participant_file(workspace, agent).unlink(missing_ok=True)

    with (
        patch.object(application, "_wait_for_registration", return_value=participants) as wait_mock,
        patch.object(application, "_clear_terminal_screen") as clear_mock,
    ):
        result = application._load_or_wait_participants(workspace)

    assert result == participants
    wait_mock.assert_called_once_with(workspace)
    clear_mock.assert_called_once_with()


def test_bind_participants_to_layout_overrides_registered_panes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("", encoding="utf-8")

    participants = SessionParticipants(
        claude=Participant(
            agent="claude",
            session_file=session_file,
            session_id="claude-session",
            tmux_pane="%99",
            cwd=workspace,
            registered_at="2026-02-23T00:00:00-05:00",
        ),
        codex=Participant(
            agent="codex",
            session_file=session_file,
            session_id="codex-session",
            tmux_pane="%98",
            cwd=workspace,
            registered_at="2026-02-23T00:00:00-05:00",
        ),
    )
    layout = PaneLayout(codex="%0", claude="%2", input="%1", sidebar="%3")

    application = ClaodexApplication()
    bound = application._bind_participants_to_layout(participants, layout)

    assert bound.claude.tmux_pane == "%2"
    assert bound.codex.tmux_pane == "%0"
    assert bound.claude.session_id == "claude-session"
    assert bound.codex.session_id == "codex-session"


def test_check_for_reregistration_swaps_session_file(tmp_path):
    """Re-registration after /resume swaps the session file and resets cursors."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    old_session = tmp_path / "old_session.jsonl"
    old_session.write_text("line1\nline2\nline3\n", encoding="utf-8")
    new_session = tmp_path / "new_session.jsonl"
    new_session.write_text("a\nb\n", encoding="utf-8")

    participants = _build_participants(workspace, old_session)

    # initialize cursors to old file positions
    read_cursor_path = read_cursor_file(workspace, "claude")
    delivery_cursor_path = delivery_cursor_file(workspace, "codex")
    write_cursor(read_cursor_path, 3)
    write_cursor(delivery_cursor_path, 3)

    # write updated participant JSON pointing at the new session file
    import json

    part_path = participant_file(workspace, "claude")
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_text(
        json.dumps(
            {
                "agent": "claude",
                "session_file": str(new_session),
                "session_id": "new-id",
                "tmux_pane": "%5",
                "cwd": str(workspace),
                "registered_at": "2026-02-24T12:00:00-05:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # build a minimal router
    from claodex.router import Router, RoutingConfig

    config = RoutingConfig(poll_seconds=1.0, turn_timeout_seconds=60)
    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda *_: None,
        pane_alive=lambda *_: True,
        config=config,
    )

    bus = _BusRecorder()
    app = ClaodexApplication()
    app._check_for_reregistration(workspace, router, bus)

    # session file should be swapped
    assert router.participants.claude.session_file == new_session
    assert router.participants.claude.session_id == "new-id"
    # pane ID preserved from original layout binding, not disk
    assert router.participants.claude.tmux_pane == "%1"
    # codex participant unchanged
    assert router.participants.codex.session_file == old_session

    # cursors reinitialized to new file line count (2 lines)
    assert read_cursor(read_cursor_path) == 2
    assert read_cursor(delivery_cursor_path) == 2

    # system event logged
    system_events = [e for e in bus.events if e["kind"] == "system"]
    assert any("re-registered" in e["message"] for e in system_events)


def test_check_for_reregistration_noop_when_unchanged(tmp_path):
    """No-op when session file has not changed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    session_file = tmp_path / "session.jsonl"
    session_file.write_text("line1\n", encoding="utf-8")

    participants = _build_participants(workspace, session_file)

    # write participant JSON with same session file
    import json

    part_path = participant_file(workspace, "claude")
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_text(
        json.dumps(
            {
                "agent": "claude",
                "session_file": str(session_file),
                "session_id": "claude-session",
                "tmux_pane": "%1",
                "cwd": str(workspace),
                "registered_at": "2026-02-23T00:00:00-05:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    from claodex.router import Router, RoutingConfig

    config = RoutingConfig(poll_seconds=1.0, turn_timeout_seconds=60)
    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda *_: None,
        pane_alive=lambda *_: True,
        config=config,
    )

    bus = _BusRecorder()
    app = ClaodexApplication()
    app._check_for_reregistration(workspace, router, bus)

    # nothing changed
    assert router.participants.claude.session_file == session_file
    assert len(bus.events) == 0


def test_run_dispatches_sidebar_mode(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()

    application = ClaodexApplication()
    with patch.object(application, "_run_sidebar", return_value=0) as run_sidebar_mock:
        exit_code = application.run(["sidebar", str(workspace)])

    assert exit_code == 0
    run_sidebar_mock.assert_called_once_with(workspace.resolve())


def test_ensure_sidebar_running_relaunches_when_not_python(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = PaneLayout(codex="%0", claude="%2", input="%1", sidebar="%3")

    application = ClaodexApplication()
    with (
        patch("claodex.cli.is_pane_alive", return_value=True),
        patch("claodex.cli.pane_current_command", return_value="bash"),
        patch("claodex.cli.start_sidebar_process") as start_sidebar_mock,
    ):
        application._ensure_sidebar_running(layout, workspace)

    start_sidebar_mock.assert_called_once_with(layout, workspace)


def test_ensure_sidebar_running_skips_restart_when_python_running(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = PaneLayout(codex="%0", claude="%2", input="%1", sidebar="%3")

    application = ClaodexApplication()
    with (
        patch("claodex.cli.is_pane_alive", return_value=True),
        patch("claodex.cli.pane_current_command", return_value="python3"),
        patch("claodex.cli.start_sidebar_process") as start_sidebar_mock,
    ):
        application._ensure_sidebar_running(layout, workspace)

    start_sidebar_mock.assert_not_called()


def test_ensure_sidebar_running_raises_for_dead_pane(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = PaneLayout(codex="%0", claude="%2", input="%1", sidebar="%3")

    application = ClaodexApplication()
    with patch("claodex.cli.is_pane_alive", return_value=False):
        with pytest.raises(ClaodexError, match="sidebar pane is not alive: %3"):
            application._ensure_sidebar_running(layout, workspace)


def test_runtime_repl_methods_do_not_call_print():
    source = inspect.getsource(cli_module.ClaodexApplication)
    tree = ast.parse(source)
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ClaodexApplication"
    )
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    runtime_methods = {
        "_run_repl",
        "_clear_watches",
        "_make_idle_callback",
        "_read_event",
        "_run_collab",
        "_halt_listener",
        "_response_latency_seconds",
        "_mark_agent_thinking",
        "_mark_agent_idle",
        "_update_metrics",
        "_log_event",
        "_open_exchange_log",
        "_append_exchange_message",
        "_close_exchange_log",
        "_emit_status",
    }

    for method_name in runtime_methods:
        method = methods[method_name]
        print_calls = [
            call
            for call in ast.walk(method)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "print"
        ]
        assert not print_calls, f"{method_name} contains print()"


def test_run_repl_status_command_emits_status_event(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("", encoding="utf-8")
    participants = _build_participants(workspace, session_file)
    application = ClaodexApplication()

    events = iter(
        [
            InputEvent(kind="submit", value="/status"),
            InputEvent(kind="quit"),
        ]
    )
    seen_buses: list[_BusRecorder] = []

    def fake_bus(*_args, **_kwargs) -> _BusRecorder:
        bus = _BusRecorder()
        seen_buses.append(bus)
        return bus

    def fake_read_event(_target: str, on_idle=None):  # noqa: ANN001
        _ = on_idle
        return next(events)

    fake_router = type("FakeRouter", (), {
        "participants": participants,
        "workspace_root": workspace,
    })()

    with (
        patch("claodex.cli.UIEventBus", side_effect=fake_bus),
        patch("claodex.cli.Router", return_value=fake_router),
        patch("claodex.cli.kill_session"),
        patch.object(application, "_read_event", side_effect=fake_read_event),
    ):
        application._run_repl(workspace, participants)

    assert len(seen_buses) == 1
    status_events = [event for event in seen_buses[0].events if event["kind"] == "status"]
    assert len(status_events) == 1
    assert "target: claude" in status_events[0]["message"]
    assert status_events[0]["target"] == "claude"
    # status now inlines data in the message instead of meta
    assert "pane=" in status_events[0]["message"]
    assert seen_buses[0].closed is True


def test_run_repl_toggle_updates_metrics_target(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("", encoding="utf-8")
    participants = _build_participants(workspace, session_file)
    application = ClaodexApplication()

    events = iter(
        [
            InputEvent(kind="toggle"),
            InputEvent(kind="quit"),
        ]
    )
    seen_targets: list[str] = []
    seen_buses: list[_BusRecorder] = []

    def fake_bus(*_args, **_kwargs) -> _BusRecorder:
        bus = _BusRecorder()
        seen_buses.append(bus)
        return bus

    def fake_read_event(target: str, on_idle=None):  # noqa: ANN001
        _ = on_idle
        seen_targets.append(target)
        return next(events)

    with (
        patch("claodex.cli.UIEventBus", side_effect=fake_bus),
        patch("claodex.cli.Router", return_value=object()),
        patch("claodex.cli.kill_session"),
        patch.object(application, "_read_event", side_effect=fake_read_event),
    ):
        application._run_repl(workspace, participants)

    assert seen_targets == ["claude", "codex"]
    assert len(seen_buses) == 1
    assert {"target": "codex"} in seen_buses[0].metric_updates
    assert seen_buses[0].closed is True


def test_run_repl_toggle_preserves_draft_as_prefill(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("", encoding="utf-8")
    participants = _build_participants(workspace, session_file)
    application = ClaodexApplication()

    events = iter(
        [
            InputEvent(kind="toggle", value="in-progress draft"),
            InputEvent(kind="quit"),
        ]
    )
    prefill_snapshots: list[str] = []

    def fake_read_event(target: str, on_idle=None):  # noqa: ANN001
        _ = on_idle
        # capture _input_prefill before _read_event would consume it
        prefill_snapshots.append(application._input_prefill)
        return next(events)

    seen_buses: list[_BusRecorder] = []

    def fake_bus(*_args, **_kwargs) -> _BusRecorder:
        bus = _BusRecorder()
        seen_buses.append(bus)
        return bus

    with (
        patch("claodex.cli.UIEventBus", side_effect=fake_bus),
        patch("claodex.cli.Router", return_value=object()),
        patch("claodex.cli.kill_session"),
        patch.object(application, "_read_event", side_effect=fake_read_event),
    ):
        application._run_repl(workspace, participants)

    # first read has no prefill; after toggle, the draft is stored for next read
    assert prefill_snapshots[0] == ""
    assert application._input_prefill == "in-progress draft"


def test_run_repl_collab_command_clears_terminal_line(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("", encoding="utf-8")
    participants = _build_participants(workspace, session_file)
    application = ClaodexApplication()

    events = iter(
        [
            InputEvent(kind="submit", value="/collab draft a plan"),
            InputEvent(kind="quit"),
        ]
    )

    def fake_read_event(_target: str, on_idle=None):  # noqa: ANN001
        _ = on_idle
        return next(events)

    with (
        patch("claodex.cli.UIEventBus", return_value=_BusRecorder()),
        patch("claodex.cli.Router", return_value=object()),
        patch("claodex.cli.kill_session"),
        patch.object(application, "_read_event", side_effect=fake_read_event),
        patch.object(application, "_run_collab") as run_collab_mock,
        patch.object(application, "_clear_terminal_line") as clear_line_mock,
    ):
        application._run_repl(workspace, participants)

    run_collab_mock.assert_called_once()
    assert clear_line_mock.call_count == 2


def test_run_repl_seeded_collab_clears_terminal_line(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("", encoding="utf-8")
    participants = _build_participants(workspace, session_file)
    application = ClaodexApplication()
    application._collab_seed = (
        PendingSend(
            target_agent="claude",
            before_cursor=0,
            sent_text="seed",
            sent_at=datetime.now(timezone.utc),
        ),
        ResponseTurn(agent="claude", text="seed response", source_cursor=1),
    )

    events = iter(
        [
            InputEvent(kind="collab_initiated"),
            InputEvent(kind="quit"),
        ]
    )

    def fake_read_event(_target: str, on_idle=None):  # noqa: ANN001
        _ = on_idle
        return next(events)

    with (
        patch("claodex.cli.UIEventBus", return_value=_BusRecorder()),
        patch("claodex.cli.Router", return_value=object()),
        patch("claodex.cli.kill_session"),
        patch.object(application, "_read_event", side_effect=fake_read_event),
        patch.object(application, "_run_collab") as run_collab_mock,
        patch.object(application, "_clear_terminal_line") as clear_line_mock,
    ):
        application._run_repl(workspace, participants)

    run_collab_mock.assert_called_once()
    assert clear_line_mock.call_count == 2


def test_clear_terminal_screen_clears_scrollback_when_tty():
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = True
        ClaodexApplication._clear_terminal_screen()

    mock_stdout.write.assert_called_once_with("\033[2J\033[H\033[3J")
    mock_stdout.flush.assert_called_once_with()


def test_halt_listener_queues_interjection_without_halting():
    """Non-/halt input during collab is queued and does not stop collab."""
    application = ClaodexApplication()
    halt_event = threading.Event()
    stop_event = threading.Event()

    def fake_read(target: str, on_idle=None, idle_interval=0.2, prefill=""):  # noqa: ANN001
        del target, on_idle, idle_interval, prefill
        stop_event.set()
        return InputEvent(kind="submit", value="please include perf numbers")

    with (
        patch("claodex.cli.InputEditor.read", side_effect=fake_read),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.isatty.return_value = True
        application._halt_listener(halt_event=halt_event, stop_event=stop_event)

    assert not halt_event.is_set()
    assert _drain_queue(application._collab_interjections) == ["please include perf numbers"]


def test_halt_listener_drops_queued_interjections_on_halt():
    """Typing /halt clears queued interjections before stopping collab."""
    application = ClaodexApplication()
    application._collab_interjections.put("queued note")
    halt_event = threading.Event()
    stop_event = threading.Event()

    def fake_read(target: str, on_idle=None, idle_interval=0.2, prefill=""):  # noqa: ANN001
        del target, on_idle, idle_interval, prefill
        return InputEvent(kind="submit", value="/halt")

    with (
        patch("claodex.cli.InputEditor.read", side_effect=fake_read),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.isatty.return_value = True
        application._halt_listener(halt_event=halt_event, stop_event=stop_event)

    assert halt_event.is_set()
    assert _drain_queue(application._collab_interjections) == []


def test_halt_listener_keyboard_interrupt_sets_halt():
    application = ClaodexApplication()
    halt_event = threading.Event()
    stop_event = threading.Event()

    with (
        patch("claodex.cli.InputEditor.read", side_effect=KeyboardInterrupt),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.isatty.return_value = True
        application._halt_listener(halt_event=halt_event, stop_event=stop_event)

    assert halt_event.is_set()


class _RouterStub:
    """Small router stub for collab control-flow tests."""

    def __init__(self) -> None:
        self.send_user_calls: list[tuple[str, str]] = []
        self.send_routed_calls: list[tuple[str, str, str, list[str] | None]] = []

    def send_user_message(self, target_agent: str, user_text: str) -> PendingSend:
        self.send_user_calls.append((target_agent, user_text))
        return PendingSend(
            target_agent=target_agent,
            before_cursor=0,
            sent_text=user_text,
            sent_at=datetime.now(timezone.utc),
        )

    def wait_for_response(self, pending: PendingSend) -> ResponseTurn:
        return ResponseTurn(agent=pending.target_agent, text="reply", source_cursor=1)

    def send_routed_message(
        self,
        target_agent: str,
        source_agent: str,
        response_text: str,
        user_interjections: list[str] | None = None,
    ) -> PendingSend:
        self.send_routed_calls.append(
            (target_agent, source_agent, response_text, user_interjections)
        )
        return PendingSend(
            target_agent=target_agent,
            before_cursor=1,
            sent_text=response_text,
            sent_at=datetime.now(timezone.utc),
        )


class _ReplRouterStub:
    """Router stub for REPL send assertions."""

    def __init__(self) -> None:
        self.sent_user_messages: list[tuple[str, str]] = []
        self.clear_latch_calls: list[tuple[str, int]] = []
        self._next_before_cursor = 0
        self.config = type("Config", (), {"turn_timeout_seconds": 18000})()

    def send_user_message(self, target_agent: str, user_text: str) -> PendingSend:
        self.sent_user_messages.append((target_agent, user_text))
        before_cursor = self._next_before_cursor
        self._next_before_cursor += 1
        return PendingSend(
            target_agent=target_agent,
            before_cursor=before_cursor,
            sent_text=user_text,
            blocks=[("user", user_text)],
            sent_at=datetime.now(timezone.utc),
        )

    def clear_poll_latch(self, agent: str, before_cursor: int) -> None:
        self.clear_latch_calls.append((agent, before_cursor))

    def poll_for_response(self, _pending: PendingSend):  # noqa: ANN001
        return None


def test_run_collab_halt_drops_remaining_interjections(tmp_path):
    """Queued interjections are discarded on halt and never auto-sent."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    application = ClaodexApplication()
    router = _RouterStub()
    request = CollabRequest(turns=3, start_agent="claude", message="do the task")

    captured_editor = None

    def fake_halt_listener(
        halt_event: threading.Event,
        stop_event: threading.Event,
        editor=None,
        bus=None,
    ) -> None:
        del stop_event, bus
        nonlocal captured_editor
        captured_editor = editor
        application._collab_interjections.put("queued while waiting")
        halt_event.set()

    application._halt_listener = fake_halt_listener  # type: ignore[method-assign]
    application._run_collab(workspace_root=workspace, router=router, request=request)

    assert router.send_user_calls == [("claude", "do the task")]
    assert captured_editor is application._editor
    assert router.send_routed_calls == []
    assert _drain_queue(application._collab_interjections) == []
    assert application._post_halt is True


def test_run_collab_logs_recv_event_for_completed_turn(tmp_path):
    """Collab responses emit recv events so sidebar turn counters advance."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    application = ClaodexApplication()
    router = _RouterStub()
    request = CollabRequest(turns=1, start_agent="claude", message="do the task")
    bus = _BusRecorder()

    def fake_halt_listener(*_args, **_kwargs):  # noqa: ANN001
        return

    application._halt_listener = fake_halt_listener  # type: ignore[method-assign]
    application._run_collab(workspace_root=workspace, router=router, request=request, bus=bus)

    recv_events = [event for event in bus.events if event["kind"] == "recv"]
    assert recv_events == [
        {
            "kind": "recv",
            "message": "<- claude (1 words)",
            "agent": "claude",
            "target": None,
            "meta": None,
        }
    ]


def test_run_collab_logs_recv_event_for_seed_turn(tmp_path):
    """Seeded collab responses also emit recv events for turn counters."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    application = ClaodexApplication()
    router = _RouterStub()
    request = CollabRequest(turns=1, start_agent="claude", message="do the task")
    bus = _BusRecorder()
    seed_turn = (
        PendingSend(
            target_agent="codex",
            before_cursor=0,
            sent_text="seed",
            sent_at=datetime.now(timezone.utc),
        ),
        ResponseTurn(agent="codex", text="seed reply", source_cursor=1),
    )

    def fake_halt_listener(*_args, **_kwargs):  # noqa: ANN001
        return

    application._halt_listener = fake_halt_listener  # type: ignore[method-assign]
    application._run_collab(
        workspace_root=workspace,
        router=router,
        request=request,
        seed_turn=seed_turn,
        bus=bus,
    )

    recv_events = [event for event in bus.events if event["kind"] == "recv"]
    assert recv_events == [
        {
            "kind": "recv",
            "message": "<- codex (2 words)",
            "agent": "codex",
            "target": None,
            "meta": None,
        }
    ]


def test_run_repl_prepends_post_halt_annotation_once(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("", encoding="utf-8")
    participants = _build_participants(workspace, session_file)
    application = ClaodexApplication()
    router = _ReplRouterStub()

    events = iter(
        [
            InputEvent(kind="submit", value="/collab draft plan"),
            InputEvent(kind="submit", value="first follow-up"),
            InputEvent(kind="submit", value="second follow-up"),
            InputEvent(kind="quit"),
        ]
    )

    def fake_read_event(_target: str, on_idle=None):  # noqa: ANN001
        _ = on_idle
        return next(events)

    def fake_run_collab(*_args, **_kwargs):  # noqa: ANN001
        application._post_halt = True

    with (
        patch("claodex.cli.UIEventBus", return_value=_BusRecorder()),
        patch("claodex.cli.Router", return_value=router),
        patch("claodex.cli.kill_session"),
        patch.object(application, "_read_event", side_effect=fake_read_event),
        patch.object(application, "_run_collab", side_effect=fake_run_collab),
        patch.object(application, "_clear_terminal_line"),
    ):
        application._run_repl(workspace, participants)

    assert len(router.sent_user_messages) == 2
    assert router.sent_user_messages[0] == (
        "claude",
        "(collab halted by user)\n\nfirst follow-up",
    )
    assert router.sent_user_messages[1] == ("claude", "second follow-up")
    assert application._post_halt is False


def test_run_repl_superseded_watch_preserves_blocks_for_seed_logs(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("", encoding="utf-8")
    participants = _build_participants(workspace, session_file)
    application = ClaodexApplication()
    router = _ReplRouterStub()

    events = iter(
        [
            InputEvent(kind="submit", value="first message"),
            InputEvent(kind="submit", value="second message"),
            InputEvent(kind="quit"),
        ]
    )

    def fake_read_event(_target: str, on_idle=None):  # noqa: ANN001
        _ = on_idle
        return next(events)

    with (
        patch("claodex.cli.UIEventBus", return_value=_BusRecorder()),
        patch("claodex.cli.Router", return_value=router),
        patch("claodex.cli.kill_session"),
        patch.object(application, "_read_event", side_effect=fake_read_event),
    ):
        application._run_repl(workspace, participants)

    assert router.sent_user_messages == [
        ("claude", "first message"),
        ("claude", "second message"),
    ]
    assert router.clear_latch_calls == [("claude", 0)]
    assert application._pending_watches["claude"].blocks == [
        ("user", "first message"),
        ("user", "second message"),
    ]


def test_run_collab_interjection_logging_ignores_routed_delta_blocks(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    application = ClaodexApplication()
    bus = _BusRecorder()
    request = CollabRequest(turns=2, start_agent="claude", message="start task")

    class _InterjectionRouterStub:
        def __init__(self, app: ClaodexApplication) -> None:
            self._app = app
            self._wait_calls = 0

        def send_user_message(self, target_agent: str, user_text: str) -> PendingSend:
            return PendingSend(
                target_agent=target_agent,
                before_cursor=0,
                sent_text=user_text,
                blocks=[("user", user_text)],
                sent_at=datetime(2026, 2, 24, 1, 0, 0, tzinfo=timezone.utc),
            )

        def wait_for_response(self, pending: PendingSend) -> ResponseTurn:
            self._wait_calls += 1
            if self._wait_calls == 1:
                self._app._collab_interjections.put("  please add tests  ")
                return ResponseTurn(
                    agent=pending.target_agent,
                    text="claude response",
                    source_cursor=1,
                    received_at=datetime(2026, 2, 24, 1, 1, 0, tzinfo=timezone.utc),
                )
            return ResponseTurn(
                agent=pending.target_agent,
                text="codex response",
                source_cursor=2,
                received_at=datetime(2026, 2, 24, 1, 2, 0, tzinfo=timezone.utc),
            )

        def send_routed_message(
            self,
            target_agent: str,
            source_agent: str,
            response_text: str,
            user_interjections: list[str] | None = None,
        ) -> PendingSend:
            assert user_interjections == ["please add tests"]
            return PendingSend(
                target_agent=target_agent,
                before_cursor=1,
                sent_text=response_text,
                blocks=[
                    ("user", "historic delta"),
                    (source_agent, response_text),
                    ("user", "please add tests"),
                ],
                sent_at=datetime(2026, 2, 24, 1, 1, 30, tzinfo=timezone.utc),
            )

    router = _InterjectionRouterStub(application)

    def fake_halt_listener(*_args, **_kwargs):  # noqa: ANN001
        return

    application._halt_listener = fake_halt_listener  # type: ignore[method-assign]
    application._run_collab(workspace_root=workspace, router=router, request=request, bus=bus)

    exchange_files = sorted((workspace / ".claodex" / "exchanges").glob("*.md"))
    assert len(exchange_files) == 1
    content = exchange_files[0].read_text(encoding="utf-8")
    assert content.count("claude response") == 1
    assert content.count("please add tests") == 1
    assert "historic delta" not in content


# -- exchange log tests --


def _make_pending(
    target: str,
    blocks: list[tuple[str, str]],
    sent_at: datetime | None = None,
) -> PendingSend:
    """Build a PendingSend with structured blocks for exchange log tests."""
    from claodex.router import render_block

    payload = "\n\n".join(render_block(src, body) for src, body in blocks)
    return PendingSend(
        target_agent=target,
        before_cursor=0,
        sent_text=payload,
        blocks=blocks,
        sent_at=sent_at or datetime(2026, 2, 24, 1, 0, 0, tzinfo=timezone.utc),
    )


def _make_response(
    agent: str,
    text: str,
    received_at: datetime | None = None,
) -> ResponseTurn:
    return ResponseTurn(
        agent=agent,
        text=text,
        source_cursor=1,
        received_at=received_at or datetime(2026, 2, 24, 1, 1, 0, tzinfo=timezone.utc),
    )


def _write_streaming_exchange_log(
    app: ClaodexApplication,
    workspace: Path,
    turn_records: list[tuple[PendingSend, ResponseTurn]],
    *,
    initial_message: str,
    started_at: datetime,
    turns: int,
    stop_reason: str,
    initiated_by: str = "user",
) -> Path:
    """Write an exchange log using open/append/close streaming helpers."""
    path, handle = app._open_exchange_log(
        workspace_root=workspace,
        initial_message=initial_message,
        started_at=started_at,
        initiated_by=initiated_by,
    )
    first_message = True
    for index, (pending, response) in enumerate(turn_records):
        blocks = pending.blocks if index == 0 else pending.blocks[1:]
        for source, body in blocks:
            first_message = app._append_exchange_message(
                handle,
                source,
                body,
                pending.sent_at,
                first_message=first_message,
            )
        first_message = app._append_exchange_message(
            handle,
            response.agent,
            response.text,
            response.received_at,
            first_message=first_message,
        )
    app._close_exchange_log(handle, turns=turns, stop_reason=stop_reason)
    return path


def test_exchange_log_basic_flow(tmp_path):
    """Two-turn collab produces deduplicated group-chat format."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = ClaodexApplication()

    t0 = datetime(2026, 2, 24, 1, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 2, 24, 1, 1, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 2, 24, 1, 2, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 2, 24, 1, 3, 0, tzinfo=timezone.utc)

    turn_records = [
        # turn 0: user sends to claude, claude responds
        (
            _make_pending("claude", [("user", "hello")], sent_at=t0),
            _make_response("claude", "hi back", received_at=t1),
        ),
        # turn 1: claude's response routed to codex (first block = peer, skipped)
        (
            _make_pending("codex", [("claude", "hi back")], sent_at=t2),
            _make_response("codex", "noted", received_at=t3),
        ),
    ]

    path = _write_streaming_exchange_log(
        app=app,
        workspace=workspace,
        turn_records=turn_records,
        initial_message="hello",
        started_at=datetime(2026, 2, 24, 1, 0, 0),
        turns=2,
        stop_reason="converged",
    )

    content = path.read_text(encoding="utf-8")

    # each message appears exactly once
    assert content.count("hello") == 2  # once in title, once in body
    assert content.count("hi back") == 1
    assert content.count("noted") == 1

    # headers present with markdown h2 source headings
    assert "## user" in content
    assert "## claude" in content
    assert "## codex" in content

    # no round-based markers
    assert "## Round" not in content
    assert "### →" not in content
    assert "### ←" not in content

    # first message has no leading separator; separators exist between messages
    assert not content.startswith("---")
    assert "\n---\n" in content
    assert "*Turns: 2 · Stop reason: converged*" in content


def test_exchange_log_user_interjections(tmp_path):
    """User interjections mid-collab appear between agent responses."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = ClaodexApplication()

    t0 = datetime(2026, 2, 24, 1, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 2, 24, 1, 1, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 2, 24, 1, 2, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 2, 24, 1, 3, 0, tzinfo=timezone.utc)

    turn_records = [
        (
            _make_pending("claude", [("user", "start")], sent_at=t0),
            _make_response("claude", "working on it", received_at=t1),
        ),
        # routed to codex with a user interjection
        (
            _make_pending(
                "codex",
                [("claude", "working on it"), ("user", "also check tests")],
                sent_at=t2,
            ),
            _make_response("codex", "done", received_at=t3),
        ),
    ]

    path = _write_streaming_exchange_log(
        app=app,
        workspace=workspace,
        turn_records=turn_records,
        initial_message="start",
        started_at=datetime(2026, 2, 24, 1, 0, 0),
        turns=2,
        stop_reason="converged",
    )

    content = path.read_text(encoding="utf-8")

    # user interjection appears once, between claude and codex responses
    assert content.count("also check tests") == 1
    # verify ordering: user message → claude → interjection → codex
    idx_claude = content.index("working on it")
    idx_interject = content.index("also check tests")
    idx_codex = content.index("done")
    assert idx_claude < idx_interject < idx_codex


def test_exchange_log_strips_signals(tmp_path):
    """[COLLAB] and [CONVERGED] are stripped from displayed text."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = ClaodexApplication()

    turn_records = [
        (
            _make_pending("claude", [("user", "go")]),
            _make_response("claude", "sounds good\n\n[COLLAB]"),
        ),
        (
            _make_pending("codex", [("claude", "sounds good\n\n[COLLAB]")]),
            _make_response("codex", "agreed\n\n[CONVERGED]"),
        ),
    ]

    path = _write_streaming_exchange_log(
        app=app,
        workspace=workspace,
        turn_records=turn_records,
        initial_message="go",
        started_at=datetime(2026, 2, 24, 1, 0, 0),
        turns=2,
        stop_reason="converged",
    )

    content = path.read_text(encoding="utf-8")
    assert "[COLLAB]" not in content
    assert "[CONVERGED]" not in content
    # actual message text survives
    assert "sounds good" in content
    assert "agreed" in content


def test_exchange_log_literal_header_in_body_not_split(tmp_path):
    """Agent text containing literal ``--- user ---`` is NOT split into blocks.

    Because we use structured blocks from PendingSend (built at send time),
    not regex parsing of sent_text, literal headers in message bodies are
    treated as plain text.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = ClaodexApplication()

    # codex's response contains a literal header pattern
    agent_text = "Here is literal:\n--- user ---\nnot a real block"

    turn_records = [
        (
            _make_pending("claude", [("user", "show me")]),
            _make_response("claude", agent_text),
        ),
    ]

    path = _write_streaming_exchange_log(
        app=app,
        workspace=workspace,
        turn_records=turn_records,
        initial_message="show me",
        started_at=datetime(2026, 2, 24, 1, 0, 0),
        turns=1,
        stop_reason="turns_reached",
    )

    content = path.read_text(encoding="utf-8")

    # the literal header text appears as part of claude's response, not as
    # a separate user message
    assert "--- user ---" in content
    # only two message headers: one for user, one for claude
    assert content.count("## user") == 1
    assert content.count("## claude") == 1


def test_exchange_log_timestamps_present(tmp_path):
    """Every message gets a local timestamp from structured data."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = ClaodexApplication()

    t0 = datetime(2026, 2, 24, 1, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 2, 24, 1, 5, 0, tzinfo=timezone.utc)

    turn_records = [
        (
            _make_pending("claude", [("user", "ping")], sent_at=t0),
            _make_response("claude", "pong", received_at=t1),
        ),
    ]

    path = _write_streaming_exchange_log(
        app=app,
        workspace=workspace,
        turn_records=turn_records,
        initial_message="ping",
        started_at=datetime(2026, 2, 24, 1, 0, 0),
        turns=1,
        stop_reason="turns_reached",
    )

    content = path.read_text(encoding="utf-8")
    # both messages should have timestamps (no bare source header without · time)
    assert "## user ·" in content
    assert "## claude ·" in content
    # should contain AM/PM formatted time
    assert "AM" in content or "PM" in content


def test_strip_routing_signals():
    assert _strip_routing_signals("hello\n\n[COLLAB]") == "hello"
    assert _strip_routing_signals("done\n\n[CONVERGED]") == "done"
    assert _strip_routing_signals("plain text") == "plain text"
    # both signals in either order
    assert _strip_routing_signals("ok\n\n[CONVERGED]\n\n[COLLAB]") == "ok"
    assert _strip_routing_signals("ok\n[COLLAB]\n[CONVERGED]") == "ok"
    # stacked duplicates
    assert _strip_routing_signals("ok\n[CONVERGED]\n[CONVERGED]") == "ok"


def test_format_local_time():
    ts = datetime(2026, 2, 24, 13, 5, 0, tzinfo=timezone.utc)
    result = _format_local_time(ts)
    # result depends on local timezone, but should match H:MM AM/PM pattern
    assert ":" in result
    assert result.endswith("AM") or result.endswith("PM")


def test_exchange_log_seed_turn_has_timestamp(tmp_path):
    """Agent-initiated collab (seed turn) still gets timestamps on every message."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = ClaodexApplication()

    t0 = datetime(2026, 2, 24, 1, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 2, 24, 1, 0, 30, tzinfo=timezone.utc)
    t2 = datetime(2026, 2, 24, 1, 1, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 2, 24, 1, 2, 0, tzinfo=timezone.utc)

    # seed turn: user sent to claude, claude responded with [COLLAB]
    seed = (
        _make_pending("claude", [("user", "review this")], sent_at=t0),
        _make_response("claude", "looks good, let me ask codex", received_at=t1),
    )

    # subsequent turn: routed to codex
    follow = (
        _make_pending(
            "codex",
            [("claude", "looks good, let me ask codex")],
            sent_at=t2,
        ),
        _make_response("codex", "agreed", received_at=t3),
    )

    path = _write_streaming_exchange_log(
        app=app,
        workspace=workspace,
        turn_records=[seed, follow],
        initial_message="review this",
        started_at=datetime(2026, 2, 24, 1, 0, 0),
        turns=2,
        stop_reason="converged",
        initiated_by="claude",
    )

    content = path.read_text(encoding="utf-8")

    # every message header must have a timestamp (no bare "## source" without · time)
    import re

    bare_headers = re.findall(r"^##\s+\w+\s*$", content, flags=re.MULTILINE)
    assert bare_headers == [], f"headers without timestamps: {bare_headers}"


# -- session name derivation tests --


def test_session_name_for_basic():
    """Session name includes sanitized directory name and path hash."""
    name = ClaodexApplication._session_name_for(Path("/home/user/my-project"))
    assert name.startswith("claodex-my-project-")
    # hash suffix is 6 hex chars
    suffix = name.split("-", 2)[-1].split("-")[-1]
    assert len(suffix) == 6


def test_session_name_for_different_paths_same_basename():
    """Same directory name in different locations produces different session names."""
    name_a = ClaodexApplication._session_name_for(Path("/tmp/a/project"))
    name_b = ClaodexApplication._session_name_for(Path("/var/tmp/project"))
    assert name_a != name_b


def test_session_name_for_dots_sanitized():
    """Dots in directory names are replaced with dashes."""
    name = ClaodexApplication._session_name_for(Path("/home/user/my.project"))
    # should not contain dots (tmux interprets them as target separators)
    prefix = name.rsplit("-", 1)[0]
    assert "." not in prefix


def test_session_name_for_root():
    """Root path (empty name) gets 'root' as dirname."""
    name = ClaodexApplication._session_name_for(Path("/"))
    assert name.startswith("claodex-root-")


# -- _home_shorthand tests --


def test_home_shorthand_under_home():
    """Paths under $HOME are shortened with ~."""
    home = Path.home()
    path = home / "codes" / "project"
    result = ClaodexApplication._home_shorthand(path)
    assert result == "~/codes/project"


def test_home_shorthand_not_under_home():
    """Paths not under $HOME are returned as-is."""
    result = ClaodexApplication._home_shorthand(Path("/tmp/project"))
    assert result == "/tmp/project"


def test_home_shorthand_prefix_boundary():
    """Paths sharing a string prefix with $HOME but not a path boundary are not shortened."""
    home = Path.home()
    # e.g. /home/master -> /home/mastering should NOT become ~ing
    fake = Path(str(home) + "ing/test")
    result = ClaodexApplication._home_shorthand(fake)
    assert result == str(fake)
    assert "~" not in result


def test_home_shorthand_exact_home():
    """$HOME itself becomes ~."""
    result = ClaodexApplication._home_shorthand(Path.home())
    assert result == "~"


# -- workspace resolution tests --


def test_resolve_workspace_non_git(tmp_path):
    """Non-git directory is accepted without error."""
    workspace = tmp_path / "plain-dir"
    workspace.mkdir()
    app = ClaodexApplication()
    result = app._resolve_workspace(workspace)
    assert result == workspace


# -- _status_line width tests --


def test_status_line_narrow_terminal():
    """Status line never exceeds terminal width for narrow terminals."""
    with patch("shutil.get_terminal_size") as mock_size:
        mock_size.return_value = type("Size", (), {"columns": 25})()
        line = ClaodexApplication._status_line("agents", "ok")
        # visible length should not exceed 25
        assert len(line) <= 25


def test_status_line_very_narrow_terminal():
    """Extremely narrow terminal gets space-separated fallback."""
    with patch("shutil.get_terminal_size") as mock_size:
        mock_size.return_value = type("Size", (), {"columns": 12})()
        line = ClaodexApplication._status_line("agents", "ok")
        assert len(line) <= 12

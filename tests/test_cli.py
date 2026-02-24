from __future__ import annotations

import ast
from datetime import datetime, timezone
import inspect
from pathlib import Path
import threading
from unittest.mock import patch

import pytest

import claodex.cli as cli_module
from claodex.cli import ClaodexApplication, CollabRequest, _drain_queue
from claodex.errors import ClaodexError
from claodex.input_editor import InputEvent
from claodex.router import PendingSend, ResponseTurn
from claodex.state import (
    Participant,
    SessionParticipants,
    delivery_cursor_file,
    participant_file,
    read_cursor_file,
    ui_events_file,
    ui_metrics_file,
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
        "_write_exchange_log",
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

    with (
        patch("claodex.cli.UIEventBus", side_effect=fake_bus),
        patch("claodex.cli.Router", return_value=object()),
        patch("claodex.cli.kill_session"),
        patch.object(application, "_read_event", side_effect=fake_read_event),
    ):
        application._run_repl(workspace, participants)

    assert len(seen_buses) == 1
    status_events = [event for event in seen_buses[0].events if event["kind"] == "status"]
    assert len(status_events) == 1
    assert status_events[0]["message"] == "status snapshot"
    assert status_events[0]["target"] == "claude"
    assert isinstance(status_events[0]["meta"], dict)
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
    clear_line_mock.assert_called_once()


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
    clear_line_mock.assert_called_once()


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
        self.config = type("Config", (), {"turn_timeout_seconds": 18000})()

    def send_user_message(self, target_agent: str, user_text: str) -> PendingSend:
        self.sent_user_messages.append((target_agent, user_text))
        return PendingSend(
            target_agent=target_agent,
            before_cursor=0,
            sent_text=user_text,
            sent_at=datetime.now(timezone.utc),
        )

    def clear_poll_latch(self, _agent: str, _before_cursor: int) -> None:
        return

    def poll_for_response(self, _pending: PendingSend):  # noqa: ANN001
        return None


def test_run_collab_halt_drops_remaining_interjections(tmp_path):
    """Queued interjections are discarded on halt and never auto-sent."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    application = ClaodexApplication()
    router = _RouterStub()
    request = CollabRequest(turns=3, start_agent="claude", message="do the task")

    application._write_exchange_log = lambda **_kwargs: workspace / "exchange.md"

    def fake_halt_listener(halt_event: threading.Event, stop_event: threading.Event) -> None:
        del stop_event
        application._collab_interjections.put("queued while waiting")
        halt_event.set()

    application._halt_listener = fake_halt_listener  # type: ignore[method-assign]
    application._run_collab(workspace_root=workspace, router=router, request=request)

    assert router.send_user_calls == [("claude", "do the task")]
    assert router.send_routed_calls == []
    assert _drain_queue(application._collab_interjections) == []
    assert application._post_halt is True


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

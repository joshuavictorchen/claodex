from __future__ import annotations

from datetime import datetime, timezone
import threading
from unittest.mock import patch

from claodex.cli import ClaodexApplication, CollabRequest, _drain_queue
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


def test_halt_listener_queues_interjection_without_halting():
    """Non-/halt input during collab is queued and does not stop collab."""
    application = ClaodexApplication()
    halt_event = threading.Event()
    stop_event = threading.Event()

    def fake_select(rlist, _wlist, _xlist, _timeout):
        return rlist, [], []

    def fake_readline() -> str:
        stop_event.set()
        return "please include perf numbers\n"

    with (
        patch("claodex.cli.select.select", side_effect=fake_select),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.isatty.return_value = True
        mock_stdin.readline.side_effect = fake_readline
        application._halt_listener(halt_event=halt_event, stop_event=stop_event)

    assert not halt_event.is_set()
    assert _drain_queue(application._collab_interjections) == ["please include perf numbers"]


def test_halt_listener_drops_queued_interjections_on_halt():
    """Typing /halt clears queued interjections before stopping collab."""
    application = ClaodexApplication()
    application._collab_interjections.put("queued note")
    halt_event = threading.Event()
    stop_event = threading.Event()

    def fake_select(rlist, _wlist, _xlist, _timeout):
        return rlist, [], []

    with (
        patch("claodex.cli.select.select", side_effect=fake_select),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.isatty.return_value = True
        mock_stdin.readline.return_value = "/halt\n"
        application._halt_listener(halt_event=halt_event, stop_event=stop_event)

    assert halt_event.is_set()
    assert _drain_queue(application._collab_interjections) == []


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

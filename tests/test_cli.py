from __future__ import annotations

from claodex.cli import ClaodexApplication
from claodex.state import (
    Participant,
    SessionParticipants,
    delivery_cursor_file,
    participant_file,
    read_cursor_file,
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

    application = ClaodexApplication()
    application._clear_session_state(workspace)

    for agent in ("claude", "codex"):
        assert not participant_file(workspace, agent).exists()
        assert not read_cursor_file(workspace, agent).exists()
        assert not delivery_cursor_file(workspace, agent).exists()


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
    layout = PaneLayout(codex="%0", claude="%2", cli="%1")

    application = ClaodexApplication()
    bound = application._bind_participants_to_layout(participants, layout)

    assert bound.claude.tmux_pane == "%2"
    assert bound.codex.tmux_pane == "%0"
    assert bound.claude.session_id == "claude-session"
    assert bound.codex.session_id == "codex-session"

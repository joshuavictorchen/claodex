from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from claodex.cli import parse_collab_request
from claodex.errors import ClaodexError
from claodex.router import PendingSend, Router, RoutingConfig, strip_injected_context
from claodex.state import (
    Participant,
    SessionParticipants,
    ensure_state_layout,
    read_delivery_cursor,
    read_read_cursor,
    write_delivery_cursor,
    write_read_cursor,
)


def _write_jsonl(path: Path, entries: list[dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            if isinstance(entry, str):
                handle.write(entry + "\n")
                continue
            handle.write(json.dumps(entry) + "\n")


def _claude_entries(user_text: str, assistant_text: str) -> list[dict]:
    return [
        {
            "timestamp": "2026-02-22T10:00:00Z",
            "type": "user",
            "sessionId": "claude-session",
            "message": {"role": "user", "content": user_text},
        },
        {
            "timestamp": "2026-02-22T10:00:01Z",
            "type": "assistant",
            "sessionId": "claude-session",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        },
    ]


def _claude_turn_entries(user_text: str, assistant_text: str) -> list[dict]:
    return [
        {
            "timestamp": "2026-02-22T10:00:00Z",
            "type": "user",
            "sessionId": "claude-session",
            "message": {"role": "user", "content": user_text},
        },
        {
            "timestamp": "2026-02-22T10:00:01Z",
            "type": "assistant",
            "sessionId": "claude-session",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        },
        {
            "timestamp": "2026-02-22T10:00:02Z",
            "type": "system",
            "subtype": "turn_duration",
            "isMeta": False,
        },
    ]


def _claude_tool_entries(tool_complete: bool) -> list[dict]:
    entries = [
        {
            "timestamp": "2026-02-22T10:00:00Z",
            "type": "user",
            "sessionId": "claude-session",
            "message": {"role": "user", "content": "run checks"},
        },
        {
            "timestamp": "2026-02-22T10:00:01Z",
            "type": "assistant",
            "sessionId": "claude-session",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "running tests now"},
                    {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"cmd": "pytest"}},
                ],
            },
        },
    ]
    if tool_complete:
        entries.extend(
            [
                {
                    "timestamp": "2026-02-22T10:00:02Z",
                    "type": "user",
                    "sessionId": "claude-session",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}],
                    },
                },
                {
                    "timestamp": "2026-02-22T10:00:03Z",
                    "type": "assistant",
                    "sessionId": "claude-session",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "tests passed"}],
                    },
                },
                {
                    "timestamp": "2026-02-22T10:00:04Z",
                    "type": "system",
                    "subtype": "turn_duration",
                    "isMeta": False,
                },
            ]
        )
    return entries


def _codex_entries(user_text: str, assistant_text: str) -> list[dict]:
    return [
        {
            "timestamp": "2026-02-22T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "codex-session", "cwd": "ignored"},
        },
        {
            "timestamp": "2026-02-22T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": user_text},
        },
        {
            "timestamp": "2026-02-22T10:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": assistant_text}],
            },
        },
    ]


def _codex_turn_entries(
    user_text: str,
    assistant_text: str,
    include_task_started: bool,
    include_task_complete: bool,
) -> list[dict]:
    entries = [
        {
            "timestamp": "2026-02-22T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "codex-session", "cwd": "ignored"},
        }
    ]
    if include_task_started:
        entries.append(
            {
                "timestamp": "2026-02-22T10:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started"},
            }
        )
    entries.extend(
        [
            {
                "timestamp": "2026-02-22T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": user_text},
            },
            {
                "timestamp": "2026-02-22T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": assistant_text}],
                },
            },
        ]
    )
    if include_task_complete:
        entries.append(
            {
                "timestamp": "2026-02-22T10:00:03Z",
                "type": "event_msg",
                "payload": {"type": "task_complete"},
            }
        )
    return entries


def _participants(workspace: Path, claude_session: Path, codex_session: Path) -> SessionParticipants:
    return SessionParticipants(
        claude=Participant(
            agent="claude",
            session_file=claude_session,
            session_id="claude-session",
            tmux_pane="%1",
            cwd=workspace,
            registered_at="2026-02-22T10:00:00-05:00",
        ),
        codex=Participant(
            agent="codex",
            session_file=codex_session,
            session_id="codex-session",
            tmux_pane="%2",
            cwd=workspace,
            registered_at="2026-02-22T10:00:00-05:00",
        ),
    )


def test_parse_collab_request_defaults():
    parsed = parse_collab_request("/collab design api", default_start="claude")
    assert parsed.turns == 10
    assert parsed.start_agent == "claude"
    assert parsed.message == "design api"


def test_parse_collab_request_with_options():
    parsed = parse_collab_request(
        "/collab --turns 4 --start codex implement auth",
        default_start="claude",
    )
    assert parsed.turns == 4
    assert parsed.start_agent == "codex"
    assert parsed.message == "implement auth"


def test_parse_collab_request_rejects_negative_turns():
    with pytest.raises(ClaodexError, match="--turns must be positive"):
        parse_collab_request("/collab --turns -1 design api", default_start="claude")


def test_parse_collab_request_requires_message():
    with pytest.raises(ClaodexError, match="/collab requires a message"):
        parse_collab_request("/collab --turns 3", default_start="claude")


def test_parse_collab_request_extra_whitespace():
    parsed = parse_collab_request(
        "/collab  --turns    4  --start  codex   hello world", default_start="claude"
    )
    assert parsed.turns == 4
    assert parsed.start_agent == "codex"
    assert parsed.message == "hello world"


def test_parse_collab_request_apostrophes_in_message():
    parsed = parse_collab_request(
        "/collab --turns 3 I'm testing it's features", default_start="claude"
    )
    assert parsed.turns == 3
    assert parsed.message == "I'm testing it's features"


def test_parse_collab_request_rejects_unknown_option():
    with pytest.raises(ClaodexError, match="unknown option"):
        parse_collab_request("/collab --turn 3 do stuff", default_start="claude")


def test_parse_collab_request_double_dash_terminates_options():
    parsed = parse_collab_request("/collab --turns 2 -- --this starts with dashes", default_start="claude")
    assert parsed.turns == 2
    assert parsed.message == "--this starts with dashes"


def test_strip_injected_context_keeps_final_user_block():
    message = """--- user ---
seed question

--- claude ---
analysis

--- user ---
final instruction
"""
    assert strip_injected_context(message) == "final instruction"


def test_send_user_message_includes_peer_delta_and_advances_delivery_cursor(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, _claude_entries("task", "done"))
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    sent_messages: list[str] = []

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: sent_messages.append(content),
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.05, turn_timeout_seconds=5),
    )

    router.send_user_message("codex", "please review")

    assert sent_messages
    assert "--- user ---\ntask" in sent_messages[0]
    assert "--- claude ---\ndone" in sent_messages[0]
    assert sent_messages[0].endswith("--- user ---\nplease review")
    assert read_delivery_cursor(workspace, "codex") == read_read_cursor(workspace, "claude")


def test_send_user_message_stamps_sent_at_before_paste(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, [])
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "claude", 3)
    write_delivery_cursor(workspace, "codex", 0)

    paste_seen_at: datetime | None = None

    def _record_paste_time(_pane: str, _content: str) -> None:
        nonlocal paste_seen_at
        paste_seen_at = datetime.now(timezone.utc)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=_record_paste_time,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=5),
    )

    pending = router.send_user_message("claude", "hello")
    assert pending.sent_at is not None
    assert paste_seen_at is not None
    assert pending.sent_at <= paste_seen_at


def test_refresh_source_skips_stuck_malformed_tail_after_retries(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, [*_claude_entries("task", "done"), "{"])
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 2)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.05, turn_timeout_seconds=5),
    )

    assert router.refresh_source("claude") == 2
    assert router.refresh_source("claude") == 2
    # third stuck refresh skips one malformed line
    assert router.refresh_source("claude") == 3
    # no additional rows remain
    assert router.refresh_source("claude") == 3


def test_wait_for_response_codex_requires_task_complete_when_started(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, _claude_entries("task", "done"))
    _write_jsonl(
        codex_session,
        _codex_turn_entries(
            user_text="question",
            assistant_text="partial response",
            include_task_started=True,
            include_task_complete=False,
        ),
    )

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 2)
    write_read_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "codex", 2)
    write_delivery_cursor(workspace, "claude", 0)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=1),
    )

    pending = PendingSend(target_agent="codex", before_cursor=0, sent_text="--- user ---\nquestion")
    with pytest.raises(ClaodexError, match="SMOKE SIGNAL: codex emitted task_started"):
        router.wait_for_response(pending=pending, timeout_seconds=0.2)


def test_wait_for_response_codex_smoke_when_assistant_has_no_markers(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, _claude_entries("task", "done"))
    _write_jsonl(
        codex_session,
        _codex_turn_entries(
            user_text="question",
            assistant_text="partial response",
            include_task_started=False,
            include_task_complete=False,
        ),
    )

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 2)
    write_read_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "codex", 2)
    write_delivery_cursor(workspace, "claude", 0)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=1),
    )

    pending = PendingSend(target_agent="codex", before_cursor=0, sent_text="--- user ---\nquestion")
    with pytest.raises(
        ClaodexError,
        match="SMOKE SIGNAL: codex emitted assistant output but no event_msg.payload.type=task_complete marker",
    ):
        router.wait_for_response(pending=pending, timeout_seconds=0.2)


def test_wait_for_response_codex_accepts_task_complete(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, _claude_entries("task", "done"))
    _write_jsonl(
        codex_session,
        _codex_turn_entries(
            user_text="question",
            assistant_text="final response",
            include_task_started=True,
            include_task_complete=True,
        ),
    )

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 2)
    write_read_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "codex", 2)
    write_delivery_cursor(workspace, "claude", 0)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=1),
    )

    pending = PendingSend(target_agent="codex", before_cursor=0, sent_text="--- user ---\nquestion")
    response = router.wait_for_response(pending=pending, timeout_seconds=0.5)
    assert response.agent == "codex"
    assert response.text == "final response"


def test_wait_for_response_codex_ignores_pre_start_task_complete(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, _claude_entries("task", "done"))
    _write_jsonl(
        codex_session,
        [
            {
                "timestamp": "2026-02-22T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "codex-session", "cwd": "ignored"},
            },
            {
                "timestamp": "2026-02-22T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "old question"},
            },
            {
                "timestamp": "2026-02-22T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "old response"}],
                },
            },
            {
                "timestamp": "2026-02-22T10:00:03Z",
                "type": "event_msg",
                "payload": {"type": "task_complete"},
            },
            {
                "timestamp": "2026-02-22T10:00:04Z",
                "type": "event_msg",
                "payload": {"type": "task_started"},
            },
            {
                "timestamp": "2026-02-22T10:00:05Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "new question"},
            },
            {
                "timestamp": "2026-02-22T10:00:06Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "new response"}],
                },
            },
        ],
    )

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 2)
    write_read_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "codex", 2)
    write_delivery_cursor(workspace, "claude", 0)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=1),
    )

    pending = PendingSend(target_agent="codex", before_cursor=0, sent_text="--- user ---\nnew question")
    with pytest.raises(ClaodexError, match="SMOKE SIGNAL: codex emitted task_started"):
        router.wait_for_response(pending=pending, timeout_seconds=0.2)


def test_wait_for_response_claude_waits_for_tool_completion(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, _claude_tool_entries(tool_complete=False))
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=1),
    )

    pending = PendingSend(target_agent="claude", before_cursor=0, sent_text="--- user ---\nrun checks")
    with pytest.raises(
        ClaodexError,
        match="SMOKE SIGNAL: claude emitted assistant output but no system.subtype=turn_duration or debug-log Stop event marker",
    ):
        router.wait_for_response(pending=pending, timeout_seconds=0.2)


def test_wait_for_response_claude_returns_after_tool_result(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, _claude_tool_entries(tool_complete=True))
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=1),
    )

    pending = PendingSend(target_agent="claude", before_cursor=0, sent_text="--- user ---\nrun checks")
    response = router.wait_for_response(pending=pending, timeout_seconds=0.5)
    assert response.agent == "claude"
    assert response.text == "tests passed"


def test_wait_for_response_claude_simple_turn_duration(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, _claude_turn_entries("design api", "simple answer"))
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=1),
    )

    pending = PendingSend(target_agent="claude", before_cursor=0, sent_text="--- user ---\ndesign api")
    response = router.wait_for_response(pending=pending, timeout_seconds=0.5)
    assert response.agent == "claude"
    assert response.text == "simple answer"


def test_wait_for_response_claude_stop_event_fallback(tmp_path):
    """Claude turn without turn_duration is detected via debug-log Stop event."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    # no turn_duration entry — just user + assistant
    _write_jsonl(claude_session, _claude_entries("hello", "hey there"))
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    # write a debug log with a Stop event
    debug_dir = tmp_path / "debug"
    debug_dir.mkdir()
    debug_log = debug_dir / "claude-session.txt"
    debug_log.write_text(
        "2099-01-01T00:00:00.000Z [DEBUG] Getting matching hook commands for Stop with query: undefined\n"
    )

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=5),
    )

    # patch the debug log path to use our tmp file
    import claodex.router as router_module
    original_pattern = router_module.CLAUDE_DEBUG_LOG_PATTERN
    router_module.CLAUDE_DEBUG_LOG_PATTERN = str(debug_log).replace("claude-session", "{session_id}")
    try:
        pending = PendingSend(target_agent="claude", before_cursor=0, sent_text="--- user ---\nhello")
        response = router.wait_for_response(pending=pending, timeout_seconds=2.0)
        assert response.agent == "claude"
        assert response.text == "hey there"
    finally:
        router_module.CLAUDE_DEBUG_LOG_PATTERN = original_pattern


def test_wait_for_response_claude_stop_event_no_assistant_text(tmp_path):
    """Stop event fires but no assistant text after anchor — should timeout, not succeed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    # only a user entry, no assistant response
    _write_jsonl(
        claude_session,
        [
            {
                "timestamp": "2026-02-22T10:00:00Z",
                "type": "user",
                "sessionId": "claude-session",
                "message": {"role": "user", "content": "hello"},
            },
        ],
    )
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    debug_dir = tmp_path / "debug"
    debug_dir.mkdir()
    debug_log = debug_dir / "claude-session.txt"
    debug_log.write_text(
        "2099-01-01T00:00:00.000Z [DEBUG] Getting matching hook commands for Stop with query: undefined\n"
    )

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=5),
    )

    import claodex.router as router_module
    original_pattern = router_module.CLAUDE_DEBUG_LOG_PATTERN
    router_module.CLAUDE_DEBUG_LOG_PATTERN = str(debug_log).replace("claude-session", "{session_id}")
    try:
        pending = PendingSend(target_agent="claude", before_cursor=0, sent_text="--- user ---\nhello")
        with pytest.raises(ClaodexError, match="SMOKE SIGNAL"):
            router.wait_for_response(pending=pending, timeout_seconds=0.3)
    finally:
        router_module.CLAUDE_DEBUG_LOG_PATTERN = original_pattern


def test_wait_for_response_claude_stop_event_ignores_stale(tmp_path):
    """Stop events from before send_time are ignored."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, _claude_entries("hello", "hey there"))
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    # write a stale Stop event from 2020
    debug_dir = tmp_path / "debug"
    debug_dir.mkdir()
    debug_log = debug_dir / "claude-session.txt"
    debug_log.write_text(
        "2020-01-01T00:00:00.000Z [DEBUG] Getting matching hook commands for Stop with query: undefined\n"
    )

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=5),
    )

    import claodex.router as router_module
    original_pattern = router_module.CLAUDE_DEBUG_LOG_PATTERN
    router_module.CLAUDE_DEBUG_LOG_PATTERN = str(debug_log).replace("claude-session", "{session_id}")
    try:
        pending = PendingSend(target_agent="claude", before_cursor=0, sent_text="--- user ---\nhello")
        # stale Stop event should be ignored — timeout
        with pytest.raises(ClaodexError, match="SMOKE SIGNAL"):
            router.wait_for_response(pending=pending, timeout_seconds=0.3)
    finally:
        router_module.CLAUDE_DEBUG_LOG_PATTERN = original_pattern


def test_wait_for_response_claude_stop_event_same_millisecond_as_send_time(tmp_path):
    """Stop event with millisecond precision is accepted for same-ms send_time."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    _write_jsonl(claude_session, _claude_entries("hello", "hey there"))
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    debug_dir = tmp_path / "debug"
    debug_dir.mkdir()
    debug_log = debug_dir / "claude-session.txt"
    debug_log.write_text(
        "2026-02-22T10:00:00.123Z [DEBUG] Getting matching hook commands for Stop with query: undefined\n"
    )

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=5),
    )

    import claodex.router as router_module
    original_pattern = router_module.CLAUDE_DEBUG_LOG_PATTERN
    router_module.CLAUDE_DEBUG_LOG_PATTERN = str(debug_log).replace("claude-session", "{session_id}")
    try:
        pending = PendingSend(
            target_agent="claude",
            before_cursor=0,
            sent_text="--- user ---\nhello",
            sent_at=datetime(2026, 2, 22, 10, 0, 0, 123900, tzinfo=timezone.utc),
        )
        response = router.wait_for_response(pending=pending, timeout_seconds=0.5)
        assert response.agent == "claude"
        assert response.text == "hey there"
    finally:
        router_module.CLAUDE_DEBUG_LOG_PATTERN = original_pattern


def test_wait_for_response_claude_interference_detection(tmp_path):
    """Unexpected user input during collab wait triggers interference error."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    # two non-meta user entries: our injected message + an accidental direct input
    _write_jsonl(
        claude_session,
        [
            {
                "timestamp": "2026-02-22T10:00:00Z",
                "type": "user",
                "sessionId": "claude-session",
                "message": {"role": "user", "content": "--- codex ---\ncollab message"},
            },
            {
                "timestamp": "2026-02-22T10:00:01Z",
                "type": "user",
                "sessionId": "claude-session",
                "message": {"role": "user", "content": "oops I typed here by accident"},
            },
            {
                "timestamp": "2026-02-22T10:00:02Z",
                "type": "assistant",
                "sessionId": "claude-session",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "wrong response"}],
                },
            },
        ],
    )
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=5),
    )

    pending = PendingSend(
        target_agent="claude", before_cursor=0, sent_text="--- codex ---\ncollab message"
    )
    with pytest.raises(ClaodexError, match="interference detected"):
        router.wait_for_response(pending=pending, timeout_seconds=0.5)


def test_wait_for_response_claude_meta_rows_not_interference(tmp_path):
    """Meta user rows (command wrappers, system reminders) do not trigger interference."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    # our injected message + a meta user row (system-reminder) + assistant response + turn_duration
    _write_jsonl(
        claude_session,
        [
            {
                "timestamp": "2026-02-22T10:00:00Z",
                "type": "user",
                "sessionId": "claude-session",
                "message": {"role": "user", "content": "--- codex ---\ncollab message"},
            },
            {
                "timestamp": "2026-02-22T10:00:01Z",
                "type": "user",
                "sessionId": "claude-session",
                "message": {"role": "user", "content": "<system-reminder>some context</system-reminder>"},
            },
            {
                "timestamp": "2026-02-22T10:00:02Z",
                "type": "assistant",
                "sessionId": "claude-session",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "correct response"}],
                },
            },
            {
                "timestamp": "2026-02-22T10:00:03Z",
                "type": "system",
                "subtype": "turn_duration",
                "isMeta": False,
            },
        ],
    )
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=5),
    )

    pending = PendingSend(
        target_agent="claude", before_cursor=0, sent_text="--- codex ---\ncollab message"
    )
    response = router.wait_for_response(pending=pending, timeout_seconds=1.0)
    assert response.agent == "claude"
    assert response.text == "correct response"


def test_wait_for_response_claude_interference_wrong_first_row(tmp_path):
    """Non-matching first user row is detected as interference (out-of-band input before anchor)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_state_layout(workspace)

    claude_session = tmp_path / "claude.jsonl"
    codex_session = tmp_path / "codex.jsonl"
    # only row is out-of-band user input that doesn't match sent_text
    _write_jsonl(
        claude_session,
        [
            {
                "timestamp": "2026-02-22T10:00:00Z",
                "type": "user",
                "sessionId": "claude-session",
                "message": {"role": "user", "content": "some unrelated question"},
            },
            {
                "timestamp": "2026-02-22T10:00:01Z",
                "type": "assistant",
                "sessionId": "claude-session",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "wrong response"}],
                },
            },
        ],
    )
    _write_jsonl(codex_session, _codex_entries("ack", "ack"))

    participants = _participants(workspace, claude_session, codex_session)
    write_read_cursor(workspace, "claude", 0)
    write_read_cursor(workspace, "codex", 3)
    write_delivery_cursor(workspace, "codex", 0)
    write_delivery_cursor(workspace, "claude", 3)

    router = Router(
        workspace_root=workspace,
        participants=participants,
        paste_content=lambda pane, content: None,
        pane_alive=lambda pane: True,
        config=RoutingConfig(poll_seconds=0.01, turn_timeout_seconds=5),
    )

    pending = PendingSend(
        target_agent="claude", before_cursor=0, sent_text="--- codex ---\ncollab message"
    )
    with pytest.raises(ClaodexError, match="interference detected"):
        router.wait_for_response(pending=pending, timeout_seconds=0.5)

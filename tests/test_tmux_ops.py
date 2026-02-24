from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claodex.errors import ClaodexError
from claodex.skill.scripts import register
from claodex.tmux_ops import (
    PaneLayout,
    _submit_delay,
    create_session,
    paste_content,
    prefill_skill_commands,
    resolve_layout,
)


def test_paste_content_uses_load_buffer_and_paste_buffer(monkeypatch):
    tmux_calls: list[list[str]] = []
    subprocess_calls: list[dict] = []

    def fake_run_tmux(args: list[str], **kwargs):
        _ = kwargs
        tmux_calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    def fake_subprocess_run(args, **kwargs):
        subprocess_calls.append({"args": args, "input": kwargs.get("input")})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("claodex.tmux_ops._run_tmux", fake_run_tmux)
    monkeypatch.setattr("claodex.tmux_ops.subprocess.run", fake_subprocess_run)
    monkeypatch.setattr("claodex.tmux_ops.time.sleep", lambda _seconds: None)

    paste_content("%1", "--- user ---\nhello")

    # load-buffer via subprocess.run with stdin input
    assert len(subprocess_calls) == 1
    assert subprocess_calls[0]["args"] == ["tmux", "load-buffer", "-"]
    assert subprocess_calls[0]["input"] == "--- user ---\nhello"

    # paste-buffer + send-keys via _run_tmux
    assert tmux_calls == [
        ["paste-buffer", "-p", "-t", "%1"],
        ["send-keys", "-t", "%1", "C-m"],
    ]


def test_paste_content_raises_when_load_buffer_fails(monkeypatch):
    def fake_subprocess_run(args, **kwargs):
        _ = (args, kwargs)
        return subprocess.CompletedProcess(
            args=["tmux", "load-buffer", "-"],
            returncode=1,
            stdout="",
            stderr="command too long",
        )

    monkeypatch.setattr("claodex.tmux_ops.subprocess.run", fake_subprocess_run)

    with pytest.raises(ClaodexError, match="command too long"):
        paste_content("%1", "x" * 50000)


def test_prefill_skill_commands_types_without_submitting(monkeypatch):
    """prefill_skill_commands types text into panes but does NOT send C-m."""
    calls: list[list[str]] = []

    def fake_run_tmux(args: list[str], **kwargs):
        _ = kwargs
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("claodex.tmux_ops._run_tmux", fake_run_tmux)

    prefill_skill_commands(PaneLayout(codex="%1", claude="%2", input="%3", sidebar="%4"))

    # should type literal text into each pane — no C-m submit
    assert calls == [
        ["send-keys", "-t", "%1", "-l", "--", "$claodex"],
        ["send-keys", "-t", "%2", "-l", "--", "/claodex"],
    ]


def test_create_session_uses_four_pane_split_sequence(monkeypatch):
    calls: list[list[str]] = []

    def fake_session_exists(_session_name: str = "claodex") -> bool:
        return False

    def fake_run_tmux(args: list[str], **kwargs):
        _ = kwargs
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("claodex.tmux_ops.session_exists", fake_session_exists)
    monkeypatch.setattr("claodex.tmux_ops._run_tmux", fake_run_tmux)
    monkeypatch.setattr(
        "claodex.tmux_ops.resolve_layout",
        lambda session_name="claodex": PaneLayout(
            codex="%1", claude="%2", input="%3", sidebar="%4"
        ),
    )

    layout = create_session(Path("/workspace"), session_name="claodex")
    assert layout == PaneLayout(codex="%1", claude="%2", input="%3", sidebar="%4")
    assert calls == [
        [
            "new-session",
            "-d",
            "-s",
            "claodex",
            "-c",
            "/workspace",
            "-n",
            "claodex",
        ],
        [
            "split-window",
            "-v",
            "-t",
            "claodex:0.0",
            "-l",
            "25%",
            "-c",
            "/workspace",
        ],
        ["split-window", "-h", "-t", "claodex:0.0", "-c", "/workspace"],
        ["split-window", "-h", "-t", "claodex:0.1", "-l", "40%", "-c", "/workspace"],
    ]


def test_resolve_layout_maps_top_and_bottom_rows(monkeypatch):
    output = "\n".join(
        [
            "%4\t0\t120\t120\t30",
            "%6\t30\t72\t48\t10",
            "%3\t0\t0\t120\t30",
            "%5\t30\t0\t72\t10",
        ]
    )

    def fake_run_tmux(args: list[str], **kwargs):
        _ = (args, kwargs)
        return subprocess.CompletedProcess(
            args=["tmux", "list-panes"],
            returncode=0,
            stdout=output,
            stderr="",
        )

    monkeypatch.setattr("claodex.tmux_ops._run_tmux", fake_run_tmux)
    layout = resolve_layout("claodex")
    assert layout == PaneLayout(codex="%3", claude="%4", input="%5", sidebar="%6")


def test_resolve_layout_requires_four_panes(monkeypatch):
    output = "\n".join(
        [
            "%1\t0\t0\t120\t30",
            "%2\t0\t120\t120\t30",
            "%3\t30\t0\t240\t10",
        ]
    )

    def fake_run_tmux(args: list[str], **kwargs):
        _ = (args, kwargs)
        return subprocess.CompletedProcess(
            args=["tmux", "list-panes"],
            returncode=0,
            stdout=output,
            stderr="",
        )

    monkeypatch.setattr("claodex.tmux_ops._run_tmux", fake_run_tmux)
    with pytest.raises(ClaodexError, match="expected 4 panes"):
        resolve_layout("claodex")


def test_detect_tmux_pane_prefers_tmux_pane_environment(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%42")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("subprocess.run should not be called when TMUX_PANE is present")

    monkeypatch.setattr(register.subprocess, "run", fail_run)
    assert register.detect_tmux_pane() == "%42"


def test_detect_tmux_pane_falls_back_to_tmux_display_message(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["tmux", "display-message", "-p", "#{pane_id}"],
            returncode=0,
            stdout="%7\n",
            stderr="",
        )

    monkeypatch.setattr(register.subprocess, "run", fake_run)
    assert register.detect_tmux_pane() == "%7"


def test_submit_delay_defaults_for_small_payload(monkeypatch):
    monkeypatch.delenv("CLAODEX_PASTE_SUBMIT_DELAY_SECONDS", raising=False)
    assert _submit_delay("x" * 2000) == pytest.approx(0.3)


def test_submit_delay_scales_for_large_payload(monkeypatch):
    monkeypatch.delenv("CLAODEX_PASTE_SUBMIT_DELAY_SECONDS", raising=False)
    assert _submit_delay("x" * 5000) == pytest.approx(0.6)


def test_submit_delay_caps_at_two_seconds(monkeypatch):
    monkeypatch.delenv("CLAODEX_PASTE_SUBMIT_DELAY_SECONDS", raising=False)
    assert _submit_delay("x" * 50000) == pytest.approx(2.0)


def test_submit_delay_honors_valid_override(monkeypatch):
    monkeypatch.setenv("CLAODEX_PASTE_SUBMIT_DELAY_SECONDS", "0.75")
    assert _submit_delay("x") == pytest.approx(0.75)


@pytest.mark.parametrize("value", ["abc", "-1", "nan", "inf", "11"])
def test_submit_delay_rejects_invalid_override(monkeypatch, value):
    monkeypatch.setenv("CLAODEX_PASTE_SUBMIT_DELAY_SECONDS", value)
    with pytest.raises(ClaodexError, match="invalid CLAODEX_PASTE_SUBMIT_DELAY_SECONDS"):
        _submit_delay("x")

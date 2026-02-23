from __future__ import annotations

import subprocess

import pytest

from claodex.errors import ClaodexError
from claodex.skill.scripts import register
from claodex.tmux_ops import PaneLayout, _submit_delay, paste_content, prefill_skill_commands


def test_paste_content_uses_literal_mode_with_double_dash(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_tmux(args: list[str], **kwargs):
        _ = kwargs
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("claodex.tmux_ops._run_tmux", fake_run_tmux)
    monkeypatch.setattr("claodex.tmux_ops.time.sleep", lambda _seconds: None)

    paste_content("%1", "--- user ---\nhello")

    assert calls == [
        ["send-keys", "-t", "%1", "-l", "--", "--- user ---\nhello"],
        ["send-keys", "-t", "%1", "C-m"],
    ]


def test_prefill_skill_commands_types_without_submitting(monkeypatch):
    """prefill_skill_commands types text into panes but does NOT send C-m."""
    calls: list[list[str]] = []

    def fake_run_tmux(args: list[str], **kwargs):
        _ = kwargs
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("claodex.tmux_ops._run_tmux", fake_run_tmux)

    prefill_skill_commands(PaneLayout(codex="%1", claude="%2", cli="%3"))

    # should type literal text into each pane — no C-m submit
    assert calls == [
        ["send-keys", "-t", "%1", "-l", "--", "$claodex"],
        ["send-keys", "-t", "%2", "-l", "--", "/claodex"],
    ]


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

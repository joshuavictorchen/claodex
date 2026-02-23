"""tmux command helpers for claodex."""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .constants import SESSION_NAME
from .errors import ClaodexError


@dataclass(frozen=True)
class PaneLayout:
    """Resolved pane ids for a claodex tmux session."""

    codex: str
    claude: str
    cli: str


def _run_tmux(args: list[str], *, capture_output: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    """Run a tmux command.

    Args:
        args: tmux subcommand argv.
        capture_output: When true, capture stdout/stderr.
        check: When true, raise on non-zero return code.

    Returns:
        Completed subprocess result.
    """
    result = subprocess.run(
        ["tmux", *args],
        text=True,
        capture_output=capture_output,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        raise ClaodexError(stderr or f"tmux command failed: {' '.join(args)}")
    return result


def ensure_dependencies() -> None:
    """Fail fast when required executables are missing."""
    missing = [binary for binary in ("tmux", "claude", "codex") if shutil.which(binary) is None]
    if missing:
        raise ClaodexError(f"missing dependency: {', '.join(missing)}")


def session_exists(session_name: str = SESSION_NAME) -> bool:
    """Return true if a tmux session name exists."""
    result = _run_tmux(["has-session", "-t", session_name], capture_output=True, check=False)
    return result.returncode == 0


def kill_session(session_name: str = SESSION_NAME) -> None:
    """Kill a tmux session and verify it is gone.

    Args:
        session_name: tmux session to kill.

    Raises:
        ClaodexError: If the session still exists after kill attempt.
    """
    if not session_exists(session_name):
        return
    _run_tmux(["kill-session", "-t", session_name], capture_output=True, check=False)
    if session_exists(session_name):
        raise ClaodexError(f"tmux session '{session_name}' survived kill attempt")


def create_session(workspace_root: Path, session_name: str = SESSION_NAME) -> PaneLayout:
    """Create a new claodex session and return pane layout.

    Layout:
        top-left: codex
        top-right: claude
        bottom: cli
    """
    if session_exists(session_name):
        raise ClaodexError(
            f"tmux session '{session_name}' already exists; use 'claodex attach' or kill the session"
        )

    _run_tmux(
        [
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(workspace_root),
            "-n",
            "claodex",
        ]
    )

    # split into top (60%) / bottom (40%) first
    # use `-l 40%` for cross-version tmux compatibility; `-p 40` fails on tmux 3.4
    _run_tmux(
        [
            "split-window",
            "-v",
            "-t",
            f"{session_name}:0.0",
            "-l",
            "40%",
            "-c",
            str(workspace_root),
        ]
    )

    # split top row into left/right panes
    _run_tmux(["split-window", "-h", "-t", f"{session_name}:0.0", "-c", str(workspace_root)])

    return resolve_layout(session_name=session_name)


def resolve_layout(session_name: str = SESSION_NAME) -> PaneLayout:
    """Resolve pane IDs from tmux geometry.

    Args:
        session_name: tmux session name.

    Returns:
        Pane ids mapped to codex/claude/cli roles.
    """
    result = _run_tmux(
        [
            "list-panes",
            "-t",
            f"{session_name}:0",
            "-F",
            "#{pane_id}\t#{pane_top}\t#{pane_left}\t#{pane_width}\t#{pane_height}",
        ]
    )

    panes: list[dict] = []
    for row in result.stdout.splitlines():
        pane_id, top, left, width, height = row.split("\t")
        panes.append(
            {
                "pane_id": pane_id,
                "top": int(top),
                "left": int(left),
                "width": int(width),
                "height": int(height),
            }
        )

    if len(panes) != 3:
        raise ClaodexError(f"expected 3 panes in session '{session_name}', found {len(panes)}")

    panes_by_top = sorted(panes, key=lambda item: (item["top"], item["left"]))
    top_row = [pane for pane in panes_by_top if pane["top"] == panes_by_top[0]["top"]]
    if len(top_row) != 2:
        raise ClaodexError("could not resolve top-row panes")

    bottom_candidates = [pane for pane in panes if pane not in top_row]
    if len(bottom_candidates) != 1:
        raise ClaodexError("could not resolve cli pane")

    top_row_sorted = sorted(top_row, key=lambda item: item["left"])
    codex_pane = top_row_sorted[0]["pane_id"]
    claude_pane = top_row_sorted[1]["pane_id"]
    cli_pane = bottom_candidates[0]["pane_id"]

    return PaneLayout(codex=codex_pane, claude=claude_pane, cli=cli_pane)


def start_agent_processes(layout: PaneLayout, workspace_root: Path) -> None:
    """Launch codex and claude in their panes.

    Args:
        layout: Session pane layout.
        workspace_root: Workspace root path.
    """
    ws = shlex_quote(str(workspace_root))
    # env -u strips inherited vars that cause agent launch failures:
    # - CLAUDECODE: claude rejects nested sessions when this is set
    # - CODEX_THREAD_ID: register.py would bind to a parent codex session
    # - CODEX_SANDBOX_ENV: avoids sandbox-mode interference
    env_prefix = "env -u CLAUDECODE -u CODEX_THREAD_ID -u CODEX_SANDBOX_ENV"
    codex_command = f"cd {ws} && {env_prefix} codex"
    claude_command = f"cd {ws} && {env_prefix} claude"
    _run_tmux(["send-keys", "-t", layout.codex, codex_command, "C-m"])
    _run_tmux(["send-keys", "-t", layout.claude, claude_command, "C-m"])


def prefill_skill_commands(layout: PaneLayout) -> None:
    """Type skill trigger commands into agent panes without submitting.

    Prepopulates `/claodex` in Claude and `$claodex` in Codex so the user
    only needs to press Enter in each pane to trigger registration.
    """
    # `-l` for literal text, `--` to prevent tmux flag interpretation
    _run_tmux(["send-keys", "-t", layout.codex, "-l", "--", "$claodex"])
    _run_tmux(["send-keys", "-t", layout.claude, "-l", "--", "/claodex"])


def is_pane_alive(pane_id: str, session_name: str = SESSION_NAME) -> bool:
    """Return true when a pane exists and is not marked dead."""
    result = _run_tmux(
        ["list-panes", "-t", session_name, "-F", "#{pane_id} #{pane_dead}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    for row in result.stdout.splitlines():
        row = row.strip()
        if not row:
            continue
        current_id, is_dead = row.split()
        if current_id == pane_id:
            return is_dead == "0"
    return False


def pane_current_command(pane_id: str, session_name: str = SESSION_NAME) -> str | None:
    """Return the foreground command running in a pane, or None if not found."""
    result = _run_tmux(
        ["list-panes", "-t", session_name, "-F", "#{pane_id} #{pane_current_command}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for row in result.stdout.splitlines():
        parts = row.strip().split(None, 1)
        if len(parts) == 2 and parts[0] == pane_id:
            return parts[1]
    return None


def _submit_delay(content: str) -> float:
    """Compute adaptive delay between paste and submit.

    The agent TUI renderer needs time to process literal keystrokes before
    it will accept C-m as submit.  For small payloads 0.3s is sufficient,
    but large payloads need proportionally more time — especially on Codex's
    TUI, which is slower to settle than Claude Code's ink/react renderer.

    Override with CLAODEX_PASTE_SUBMIT_DELAY_SECONDS to force a fixed value.

    Args:
        content: Payload that was just pasted.

    Returns:
        Delay in seconds.
    """
    import os

    override = os.environ.get("CLAODEX_PASTE_SUBMIT_DELAY_SECONDS")
    if override is not None:
        try:
            value = float(override)
        except (ValueError, OverflowError):
            value = float("nan")
        if not (0 <= value <= 10):
            raise ClaodexError(
                f"invalid CLAODEX_PASTE_SUBMIT_DELAY_SECONDS: {override!r} "
                f"(must be a number between 0 and 10)"
            )
        return value

    # base 0.3s covers payloads up to ~2000 chars comfortably;
    # add 0.1s per additional 1000 chars, capped at 2s
    base = 0.3
    extra = max(0, len(content) - 2000) / 1000 * 0.1
    return min(base + extra, 2.0)


def paste_content(pane_id: str, content: str) -> None:
    """Send content into a pane as literal keystrokes and submit.

    Uses send-keys -l (literal mode) instead of load-buffer/paste-buffer,
    because paste-buffer doesn't work reliably with Claude and Codex TUIs.

    Args:
        pane_id: Target pane id.
        content: Message to inject.
    """
    # `--` prevents payloads that start with `-` (e.g. `--- user ---`)
    # from being parsed as tmux flags
    _run_tmux(["send-keys", "-t", pane_id, "-l", "--", content])
    time.sleep(_submit_delay(content))
    _run_tmux(["send-keys", "-t", pane_id, "C-m"])


def attach_cli_pane(layout: PaneLayout, session_name: str = SESSION_NAME) -> None:
    """Focus the CLI pane to keep user input in the bottom pane."""
    _run_tmux(["select-pane", "-t", layout.cli])
    _run_tmux(["display-message", "-t", f"{session_name}:0", "claodex ready"])


def shlex_quote(value: str) -> str:
    """Return shell-safe single-quoted string."""
    return "'" + value.replace("'", "'\\''") + "'"

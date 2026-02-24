"""tmux command helpers for claodex."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .constants import LAYOUT_BOTTOM_PERCENT, LAYOUT_SIDEBAR_PERCENT, SESSION_NAME
from .errors import ClaodexError


@dataclass(frozen=True)
class PaneLayout:
    """Resolved pane ids for a claodex tmux session."""

    codex: str
    claude: str
    input: str
    sidebar: str


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
        bottom-left: input
        bottom-right: sidebar
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

    # split into top / bottom — use `-l N%` for cross-version tmux compatibility
    _run_tmux(
        [
            "split-window",
            "-v",
            "-t",
            f"{session_name}:0.0",
            "-l",
            f"{LAYOUT_BOTTOM_PERCENT}%",
            "-c",
            str(workspace_root),
        ]
    )

    # resolve top/bottom pane ids after the initial vertical split so
    # later horizontal splits target stable pane ids instead of unstable
    # pane indexes.
    pane_rows = _run_tmux(
        [
            "list-panes",
            "-t",
            f"{session_name}:0",
            "-F",
            "#{pane_id}\t#{pane_top}",
        ]
    ).stdout.splitlines()
    if len(pane_rows) != 2:
        raise ClaodexError(
            f"expected 2 panes after initial split in session '{session_name}', found {len(pane_rows)}"
        )

    top_pane_id = ""
    bottom_pane_id = ""
    top_position: int | None = None
    bottom_position: int | None = None
    for row in pane_rows:
        pane_id, pane_top = row.split("\t")
        top_value = int(pane_top)
        if top_position is None or top_value < top_position:
            top_position = top_value
            top_pane_id = pane_id
        if bottom_position is None or top_value > bottom_position:
            bottom_position = top_value
            bottom_pane_id = pane_id

    if top_pane_id == bottom_pane_id:
        raise ClaodexError(f"could not resolve top/bottom panes in session '{session_name}'")

    # split top row into left/right panes
    _run_tmux(["split-window", "-h", "-t", top_pane_id, "-c", str(workspace_root)])
    # split bottom row into input (left) / sidebar (right)
    _run_tmux(
        [
            "split-window",
            "-h",
            "-t",
            bottom_pane_id,
            "-l",
            f"{LAYOUT_SIDEBAR_PERCENT}%",
            "-c",
            str(workspace_root),
        ]
    )

    return resolve_layout(session_name=session_name)


def resolve_layout(session_name: str = SESSION_NAME) -> PaneLayout:
    """Resolve pane IDs from tmux geometry.

    Args:
        session_name: tmux session name.

    Returns:
        Pane ids mapped to codex/claude/input/sidebar roles.
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

    if len(panes) != 4:
        raise ClaodexError(f"expected 4 panes in session '{session_name}', found {len(panes)}")

    rows: dict[int, list[dict]] = {}
    for pane in panes:
        rows.setdefault(pane["top"], []).append(pane)

    if len(rows) != 2:
        raise ClaodexError("could not resolve pane rows")

    ordered_rows = sorted(rows.items(), key=lambda item: item[0])
    top_row = ordered_rows[0][1]
    bottom_row = ordered_rows[1][1]

    if len(top_row) != 2:
        raise ClaodexError("could not resolve top-row panes")
    if len(bottom_row) != 2:
        raise ClaodexError("could not resolve bottom-row panes")

    top_row_sorted = sorted(top_row, key=lambda item: item["left"])
    bottom_row_sorted = sorted(bottom_row, key=lambda item: item["left"])

    codex_pane = top_row_sorted[0]["pane_id"]
    claude_pane = top_row_sorted[1]["pane_id"]
    input_pane = bottom_row_sorted[0]["pane_id"]
    sidebar_pane = bottom_row_sorted[1]["pane_id"]

    return PaneLayout(
        codex=codex_pane,
        claude=claude_pane,
        input=input_pane,
        sidebar=sidebar_pane,
    )


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


def start_sidebar_process(layout: PaneLayout, workspace_root: Path) -> None:
    """Launch the sidebar process in the sidebar pane."""
    exe = shlex_quote(sys.executable)
    ws = shlex_quote(str(workspace_root))
    command = f"{exe} -m claodex sidebar {ws}"
    _run_tmux(["send-keys", "-t", layout.sidebar, command, "C-m"])


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

    Even with atomic tmux paste-buffer delivery, target TUIs may need brief
    settle time before accepting C-m as submit.

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
    """Paste content into a pane and submit.

    Uses tmux load-buffer (stdin) + paste-buffer -p for atomic delivery.
    The -p flag is critical: without it tmux wraps content in
    bracketed-paste escapes (ESC[200~ / ESC[201~) which Codex's TUI
    intercepts and renders as "[Pasted Content N chars]" summaries instead
    of inserting the text.

    An earlier approach used set-buffer with the content as a CLI argument,
    which hit tmux's ~16 KB command-length limit when peer deltas were
    large ("command too long").  load-buffer from stdin has no such limit.

    Args:
        pane_id: Target pane id.
        content: Message to inject.
    """
    # load-buffer from stdin avoids the ~16 KB CLI argument limit that
    # set-buffer hits on large peer deltas
    result = subprocess.run(
        ["tmux", "load-buffer", "-"],
        input=content,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise ClaodexError(stderr or "tmux load-buffer failed")
    # -p skips bracketed-paste escapes that TUIs intercept and mangle
    _run_tmux(["paste-buffer", "-p", "-t", pane_id])
    time.sleep(_submit_delay(content))
    _run_tmux(["send-keys", "-t", pane_id, "C-m"])


def attach_cli_pane(layout: PaneLayout, session_name: str = SESSION_NAME) -> None:
    """Focus the CLI pane to keep user input in the bottom pane."""
    _run_tmux(["select-pane", "-t", layout.input])
    _run_tmux(["display-message", "-t", f"{session_name}:0", "claodex ready"])


def shlex_quote(value: str) -> str:
    """Return shell-safe single-quoted string."""
    return "'" + value.replace("'", "'\\''") + "'"

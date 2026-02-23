#!/usr/bin/env python3
"""Register agent session metadata for claodex."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

AGENTS = ("claude", "codex")


class RegisterError(RuntimeError):
    """Raised when registration cannot complete."""


def resolve_workspace_root(path: Path) -> Path:
    """Resolve git top-level from a candidate path.

    Args:
        path: Current working directory.

    Returns:
        Git root when available, else resolved path.
    """
    resolved = path.resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=resolved,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return resolved


def encode_claude_project_dir(workspace_root: str) -> str:
    """Encode workspace path with Claude project conventions.

    Args:
        workspace_root: Absolute workspace path.

    Returns:
        Encoded project directory key.
    """
    return workspace_root.replace("/", "-")


def discover_claude_session(workspace_root: Path) -> Path | None:
    """Find most recent Claude session for workspace.

    Args:
        workspace_root: Workspace root path.

    Returns:
        Matching session file or None.
    """
    project_dir = (
        Path.home()
        / ".claude"
        / "projects"
        / encode_claude_project_dir(str(workspace_root))
    )
    if not project_dir.is_dir():
        return None

    candidates: list[tuple[Path, float]] = []
    for session_file in project_dir.glob("*.jsonl"):
        try:
            candidates.append((session_file, session_file.stat().st_mtime))
        except OSError:
            continue

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][0]


def _read_session_meta(path: Path) -> dict | None:
    """Read first codex session_meta row.

    Args:
        path: Codex jsonl session file.

    Returns:
        session_meta row or None.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(100):
                raw = handle.readline()
                if not raw:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "session_meta":
                    return payload
    except OSError:
        return None
    return None


def discover_codex_session(workspace_root: Path, thread_id: str | None) -> Path | None:
    """Find best matching Codex session.

    Args:
        workspace_root: Workspace root path.
        thread_id: Optional preferred thread id.

    Returns:
        Matching session file or None.
    """
    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.is_dir():
        return None

    candidates: list[tuple[Path, float]] = []
    for session_file in sessions_root.rglob("*.jsonl"):
        try:
            candidates.append((session_file, session_file.stat().st_mtime))
        except OSError:
            continue

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[1], reverse=True)

    if thread_id:
        for session_file, _ in candidates:
            if thread_id in session_file.name:
                return session_file

        for session_file, _ in candidates:
            session_meta = _read_session_meta(session_file)
            if not session_meta:
                continue
            payload = session_meta.get("payload", {})
            if isinstance(payload, dict) and payload.get("id") == thread_id:
                return session_file

    workspace_text = str(workspace_root)
    for session_file, _ in candidates:
        session_meta = _read_session_meta(session_file)
        if not session_meta:
            continue
        payload = session_meta.get("payload", {})
        if isinstance(payload, dict) and payload.get("cwd") == workspace_text:
            return session_file

    return None


def discover_session(agent: str, workspace_root: Path) -> Path:
    """Discover active session file for an agent.

    Uses a "most recently modified" heuristic. This is safe when called from
    inside the agent's own skill trigger: the agent's JSONL was just written
    to (processing the user's skill command), so it will be the freshest file.
    The outer session running claodex is idle (in a sleep/poll loop) and its
    JSONL will be older.

    Args:
        agent: `claude` or `codex`.
        workspace_root: Workspace root path.

    Returns:
        Resolved session file path.
    """
    if agent == "claude":
        session = discover_claude_session(workspace_root)
    else:
        session = discover_codex_session(workspace_root, os.getenv("CODEX_THREAD_ID"))

    if session is None:
        raise RegisterError(f"no {agent} session found for workspace: {workspace_root}")
    return session.resolve()


def extract_claude_session_id(session_file: Path) -> str | None:
    """Extract session id from Claude transcript.

    Args:
        session_file: Session file path.

    Returns:
        session id or None.
    """
    try:
        with session_file.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(400):
                raw = handle.readline()
                if not raw:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue

                if payload.get("type") == "user":
                    session_id = payload.get("sessionId")
                    if isinstance(session_id, str) and session_id:
                        return session_id

                session_id = payload.get("sessionId")
                if isinstance(session_id, str) and session_id:
                    return session_id
    except OSError:
        return None

    return None


def extract_codex_session_id(session_file: Path) -> str | None:
    """Extract session id from codex session_meta row.

    Args:
        session_file: Session file path.

    Returns:
        session id or None.
    """
    session_meta = _read_session_meta(session_file)
    if not session_meta:
        return None
    payload = session_meta.get("payload", {})
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("id")
    if isinstance(session_id, str) and session_id:
        return session_id
    return None


def extract_session_id(agent: str, session_file: Path) -> str:
    """Extract session id with filename fallback.

    Args:
        agent: Agent identity.
        session_file: Session path.

    Returns:
        Session id string.
    """
    if agent == "claude":
        session_id = extract_claude_session_id(session_file)
    else:
        session_id = extract_codex_session_id(session_file)

    if session_id:
        return session_id
    return session_file.stem


def detect_tmux_pane() -> str:
    """Detect current tmux pane id.

    Prefers TMUX_PANE env var (set by the CLI when running registration as
    a subprocess) over querying tmux, which returns the *active* pane rather
    than the pane the agent lives in.

    Returns:
        tmux pane id string.
    """
    pane_from_env = os.getenv("TMUX_PANE", "").strip()
    if pane_from_env:
        return pane_from_env

    result = subprocess.run(
        ["tmux", "display-message", "-p", "#{pane_id}"],
        text=True,
        capture_output=True,
        check=False,
    )
    pane = result.stdout.strip()
    if result.returncode != 0 or not pane:
        stderr = result.stderr.strip()
        raise RegisterError(stderr or "failed to detect tmux pane")
    return pane


def write_registration(
    workspace_root: Path,
    agent: str,
    session_file: Path,
    session_id: str,
    tmux_pane: str,
) -> Path:
    """Write participant registration payload.

    Args:
        workspace_root: Workspace root path.
        agent: Agent identity.
        session_file: Session file path.
        session_id: Source session id.
        tmux_pane: tmux pane id.

    Returns:
        Written participant file path.
    """
    participant_dir = workspace_root / ".claodex" / "participants"
    participant_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "agent": agent,
        "session_file": str(session_file),
        "session_id": session_id,
        "tmux_pane": tmux_pane,
        "cwd": str(workspace_root),
        "registered_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
    }

    output_path = participant_dir / f"{agent}.json"
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser.

    Returns:
        Configured parser.
    """
    parser = argparse.ArgumentParser(description="claodex participant registration")
    parser.add_argument("--agent", choices=AGENTS, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run registration flow.

    Args:
        argv: Optional argv vector.

    Returns:
        Exit status code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    workspace_root = resolve_workspace_root(Path.cwd())

    try:
        session_file = discover_session(args.agent, workspace_root)
        session_id = extract_session_id(args.agent, session_file)
        tmux_pane = detect_tmux_pane()
        write_registration(
            workspace_root=workspace_root,
            agent=args.agent,
            session_file=session_file,
            session_id=session_id,
            tmux_pane=tmux_pane,
        )
    except RegisterError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"registered {args.agent}: {session_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""claodex CLI entrypoint and interactive loop."""

from __future__ import annotations

import os
import queue
import shlex
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .constants import (
    AGENTS,
    COLLAB_SIGNAL,
    CONVERGE_SIGNAL,
    DEFAULT_COLLAB_TURNS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_TURN_TIMEOUT_SECONDS,
    SESSION_NAME,
)
from .errors import ClaodexError
from .extract import ExtractionError, resolve_workspace_root
from .input_editor import InputEditor
from .router import PendingSend, ResponseTurn, Router, RoutingConfig, count_words
from .state import (
    Participant,
    SessionParticipants,
    clear_ui_state_files,
    count_lines,
    cursor_snapshot,
    delivery_cursor_file,
    ensure_claodex_gitignore,
    ensure_state_layout,
    exchanges_dir,
    initialize_cursors_from_line_counts,
    load_participant,
    load_participants,
    participant_file,
    peer_agent,
    read_cursor_file,
    write_delivery_cursor,
    write_read_cursor,
)
from .tmux_ops import (
    PaneLayout,
    attach_cli_pane,
    create_session,
    ensure_dependencies,
    is_pane_alive,
    kill_session,
    pane_current_command,
    paste_content,
    prefill_skill_commands,
    resolve_layout,
    session_exists,
    start_agent_processes,
    start_sidebar_process,
)
from .ui import UIEventBus


def _last_line_is(text: str, signal: str) -> bool:
    """Check if the last non-empty line of *text* equals *signal*."""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped == signal
    return False


def _strip_trailing_signal(text: str, signal: str) -> str:
    """Remove the last non-empty line if it matches *signal*."""
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            if lines[i].strip() == signal:
                lines.pop(i)
            break
    return "\n".join(lines).rstrip()


def _drain_queue(q: queue.Queue[str]) -> list[str]:
    """Drain all items from a queue without blocking."""
    items: list[str] = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items



def _strip_routing_signals(text: str) -> str:
    """Strip all trailing [COLLAB] and [CONVERGED] signals from message text.

    Loops until no trailing signal remains, so order and stacking don't matter.
    """
    signals = (COLLAB_SIGNAL, CONVERGE_SIGNAL)
    result = text.rstrip()
    changed = True
    while changed:
        changed = False
        for signal in signals:
            stripped = _strip_trailing_signal(result, signal)
            if stripped != result:
                result = stripped.rstrip()
                changed = True
    return result


def _format_local_time(ts: datetime) -> str:
    """Format a datetime as local ``H:MM AM/PM``."""
    local = ts.astimezone()
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{local.strftime('%M %p')}"


@dataclass(frozen=True)
class CollabRequest:
    """Parsed `/collab` command payload."""

    turns: int
    start_agent: str
    message: str


class ClaodexApplication:
    """Application coordinator for startup, attach, and REPL."""

    def __init__(self) -> None:
        """Initialize application defaults."""
        self._editor = InputEditor()
        self._pending_watches: dict[str, PendingSend] = {}
        self._collab_seed: tuple[PendingSend, ResponseTurn] | None = None
        self._collab_interjections: queue.Queue[str] = queue.Queue()
        self._input_prefill: str = ""
        self._post_halt: bool = False
        self._session_name: str = SESSION_NAME

    @staticmethod
    def _session_name_for(workspace_root: Path) -> str:
        """Derive a tmux session name from the workspace path.

        Uses the directory name plus a short hash of the full path,
        sanitized for tmux (no dots or colons). This allows multiple
        concurrent instances even for directories with the same basename
        in different locations.
        """
        import hashlib

        dirname = workspace_root.name or "root"
        # tmux session names cannot contain dots or colons
        sanitized = dirname.replace(".", "-").replace(":", "-")
        # short hash of full path to disambiguate same-named dirs
        path_hash = hashlib.sha1(
            str(workspace_root).encode(), usedforsecurity=False
        ).hexdigest()[:6]
        return f"claodex-{sanitized}-{path_hash}"

    def run(self, argv: list[str]) -> int:
        """Run the CLI from argv.

        Args:
            argv: CLI args excluding program name.

        Returns:
            Exit status code.
        """
        if argv and argv[0] in {"-h", "--help"}:
            self._print_help()
            return 0

        mode = "start"
        directory_arg = argv[0] if argv else "."
        if argv and argv[0] == "attach":
            mode = "attach"
            directory_arg = argv[1] if len(argv) > 1 else "."
        if argv and argv[0] == "sidebar":
            mode = "sidebar"
            directory_arg = argv[1] if len(argv) > 1 else "."

        workspace_root = self._resolve_workspace(Path(directory_arg))
        self._session_name = self._session_name_for(workspace_root)

        try:
            if mode == "start":
                return self._run_start(workspace_root)
            if mode == "attach":
                return self._run_attach(workspace_root)
            return self._run_sidebar(workspace_root)
        except ClaodexError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except ExtractionError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    def _resolve_workspace(self, directory: Path) -> Path:
        """Resolve and validate workspace root.

        Args:
            directory: User-provided directory argument.

        Returns:
            Workspace root path.
        """
        candidate = directory.expanduser().resolve()
        return resolve_workspace_root(candidate)

    def _run_start(self, workspace_root: Path) -> int:
        """Run startup flow then attach to tmux.

        Startup sequence:
        1. clear stale participant files
        2. create tmux session with 4 panes
        3. launch agent processes (codex, claude)
        4. wait for agents to be ready (pane command transition)
        5. prepopulate skill commands in each pane (user presses Enter)
        6. paste REPL command into input pane
        7. attach to tmux — user presses Enter in agent panes to register

        Args:
            workspace_root: Workspace root path.

        Returns:
            Exit status code.
        """
        ensure_dependencies()
        if session_exists(self._session_name):
            raise ClaodexError(
                f"tmux session '{self._session_name}' already exists; "
                "run 'claodex attach' or kill the session"
            )

        ensure_state_layout(workspace_root)
        ensure_claodex_gitignore(workspace_root)
        print()
        self._install_skill_assets()
        print(self._status_line("skill assets", "ok"))

        # clear all stale state from prior sessions: participant files (so
        # registration wait won't accept leftovers) and cursor files (so
        # attach mode will reinitialize rather than reuse stale positions)
        self._clear_session_state(workspace_root)

        created_session = False
        try:
            layout = create_session(workspace_root, session_name=self._session_name)
            created_session = True
            print(self._status_line("tmux session", "ok"))
            print(
                "    "
                f"codex={layout.codex}  claude={layout.claude}  "
                f"input={layout.input}  sidebar={layout.sidebar}"
            )

            start_sidebar_process(layout, workspace_root)
            print(self._status_line("sidebar", "ok"))

            # capture baseline shell commands before launching agents
            baseline = {
                "codex": pane_current_command(layout.codex),
                "claude": pane_current_command(layout.claude),
            }
            start_agent_processes(layout, workspace_root)
            self._wait_for_agents_ready(layout, baseline)
            print(self._status_line("agents", "ok"))

            self._write_status_line(self._status_line("skill commands", ".."))
            prefill_skill_commands(layout)
            self._clear_terminal_line()
            print(self._status_line("skill commands", "ok"))

            print()
            print("  attaching")

            attach_cli_pane(layout, session_name=self._session_name)
            exe = shlex.quote(sys.executable)
            ws = shlex.quote(str(workspace_root))
            repl_cmd = f"{exe} -m claodex attach {ws}"
            paste_content(layout.input, repl_cmd)

            # hand the user's terminal to tmux
            if os.environ.get("TMUX"):
                os.execvp("tmux", ["tmux", "switch-client", "-t", self._session_name])
            else:
                os.execvp("tmux", ["tmux", "attach-session", "-t", self._session_name])
        except Exception:
            if created_session:
                kill_session(self._session_name)
            raise

    @staticmethod
    def _run_sidebar(workspace_root: Path) -> int:
        """Run the standalone sidebar process."""
        from .sidebar import run_sidebar

        return run_sidebar(workspace_root)

    def _run_attach(self, workspace_root: Path) -> int:
        """Attach to existing claodex tmux session.

        Handles two cases:
        - Fresh start: registration hasn't happened yet; wait for user to
          complete registration, then init cursors.
        - Reattach: participants already registered; validate and resume.

        Args:
            workspace_root: Workspace root path.

        Returns:
            Exit status code.
        """
        ensure_dependencies()
        if not session_exists(self._session_name):
            raise ClaodexError(f"tmux session '{self._session_name}' does not exist")

        layout = resolve_layout(self._session_name)
        participants = self._load_or_wait_participants(workspace_root)
        participants = self._bind_participants_to_layout(participants, layout)
        self._validate_registered_panes(participants)
        self._ensure_sidebar_running(layout, workspace_root)

        # init cursors on fresh start, validate on reattach
        if self._cursors_missing(workspace_root):
            initialize_cursors_from_line_counts(workspace_root, participants)
        else:
            self._ensure_cursor_files_exist(workspace_root)

        attach_cli_pane(layout, session_name=self._session_name)

        self._run_repl(workspace_root, participants)
        return 0

    def _load_or_wait_participants(self, workspace_root: Path) -> SessionParticipants:
        """Load participants, waiting for registration if needed.

        On fresh start the REPL may launch before both agent participant
        files exist. Falls back to polling wait only when files are absent;
        malformed files still raise immediately.

        Args:
            workspace_root: Workspace root path.

        Returns:
            Loaded participant metadata.
        """
        # check whether both participant files exist before attempting load
        both_exist = all(
            participant_file(workspace_root, agent).exists() for agent in AGENTS
        )
        if both_exist:
            # files exist — load and let parse/validation errors propagate
            return load_participants(workspace_root)

        # the initial prompt is immediately redrawn by _rewrite_status_block,
        # but print it once so there's no blank flash
        prompt_text = "press Enter in each agent pane to invoke the skill and register"
        if sys.stdout.isatty():
            print(f"\n  {self._ANSI_DIM}{prompt_text}{self._ANSI_RESET}")
        else:
            print(f"\n  {prompt_text}")
        participants = self._wait_for_registration(workspace_root)
        self._clear_terminal_screen()
        return participants

    def _cursors_missing(self, workspace_root: Path) -> bool:
        """Check whether any cursor files are absent.

        Args:
            workspace_root: Workspace root path.

        Returns:
            True when at least one cursor file is missing.
        """
        for agent in AGENTS:
            if not read_cursor_file(workspace_root, agent).exists():
                return True
        for target in AGENTS:
            if not delivery_cursor_file(workspace_root, target).exists():
                return True
        return False

    def _bind_participants_to_layout(
        self,
        participants: SessionParticipants,
        layout: PaneLayout,
    ) -> SessionParticipants:
        """Override participant pane IDs with current tmux layout bindings.

        Registration scripts may detect pane IDs incorrectly when running in
        sandbox or subprocess contexts. Routing always trusts the live layout.
        """

        def bind(participant: Participant, pane_id: str) -> Participant:
            if participant.tmux_pane != pane_id:
                print(
                    f"  note: overriding {participant.agent} pane "
                    f"{participant.tmux_pane} -> {pane_id}"
                )
            return Participant(
                agent=participant.agent,
                session_file=participant.session_file,
                session_id=participant.session_id,
                tmux_pane=pane_id,
                cwd=participant.cwd,
                registered_at=participant.registered_at,
            )

        return SessionParticipants(
            claude=bind(participants.claude, layout.claude),
            codex=bind(participants.codex, layout.codex),
        )

    def _ensure_cursor_files_exist(self, workspace_root: Path) -> None:
        """Require existing cursor files for attach mode.

        Args:
            workspace_root: Workspace root path.
        """
        missing: list[Path] = []
        for agent in AGENTS:
            candidate = read_cursor_file(workspace_root, agent)
            if not candidate.exists():
                missing.append(candidate)
        for target in AGENTS:
            candidate = delivery_cursor_file(workspace_root, target)
            if not candidate.exists():
                missing.append(candidate)
        if missing:
            joined = ", ".join(str(path) for path in missing)
            raise ClaodexError(f"attach requires existing cursor files; missing: {joined}")

    def _validate_registered_panes(self, participants: SessionParticipants) -> None:
        """Fail when registered panes are dead.

        Args:
            participants: Loaded participant metadata.
        """
        dead: list[str] = []
        for participant in (participants.claude, participants.codex):
            if not is_pane_alive(participant.tmux_pane):
                dead.append(f"{participant.agent} ({participant.tmux_pane})")
        if dead:
            raise ClaodexError(f"registered panes are not alive: {', '.join(dead)}")

    def _check_for_reregistration(
        self,
        workspace_root: Path,
        router: "Router",
        bus: UIEventBus | None,
    ) -> None:
        """Detect agent re-registration and hot-swap session files.

        After `/resume`, an agent writes to a new JSONL file. When the
        user re-invokes the skill, register.py updates the participant
        JSON on disk. This method detects the change and swaps the
        router's in-memory participant to point at the new file,
        reinitializing cursors so the router doesn't read stale offsets.
        """
        for agent in AGENTS:
            path = participant_file(workspace_root, agent)
            if not path.exists():
                continue
            try:
                disk = load_participant(workspace_root, agent)
            except ClaodexError:
                continue

            current = router.participants.for_agent(agent)
            if disk.session_file == current.session_file:
                continue

            # session file changed — build new participant preserving
            # the pane ID from the current layout binding
            updated = Participant(
                agent=agent,
                session_file=disk.session_file,
                session_id=disk.session_id,
                tmux_pane=current.tmux_pane,
                cwd=disk.cwd,
                registered_at=disk.registered_at,
            )

            if agent == "claude":
                router.participants = SessionParticipants(
                    claude=updated, codex=router.participants.codex
                )
            else:
                router.participants = SessionParticipants(
                    claude=router.participants.claude, codex=updated
                )

            # reinitialize cursors for the changed agent's file:
            # - read cursor: how far we've read this agent's new file
            # - delivery cursor to peer: how far in this agent's file
            #   has been delivered to the peer
            new_lines = count_lines(updated.session_file)
            peer = peer_agent(agent)
            write_read_cursor(workspace_root, agent, new_lines)
            write_delivery_cursor(workspace_root, peer, new_lines)

            # clear stale router state for this agent
            router._stuck_state.pop(agent, None)

            # clear pending watches that reference old cursor positions
            if agent in self._pending_watches:
                old = self._pending_watches.pop(agent)
                router.clear_poll_latch(agent, old.before_cursor)

            self._log_event(
                bus,
                "system",
                f"{agent} re-registered — session file swapped",
            )

    def _ensure_sidebar_running(self, layout: PaneLayout, workspace_root: Path) -> None:
        """Ensure sidebar pane is alive and running the sidebar process.

        Args:
            layout: Resolved tmux pane layout.
            workspace_root: Workspace root path.
        """
        if not is_pane_alive(layout.sidebar):
            raise ClaodexError(f"sidebar pane is not alive: {layout.sidebar}")

        current = pane_current_command(layout.sidebar)
        if current is not None and current.startswith("python"):
            return

        print("sidebar process not running; relaunching...")
        start_sidebar_process(layout, workspace_root)

    def _wait_for_agents_ready(
        self, layout: PaneLayout, baseline: dict[str, str | None]
    ) -> None:
        """Wait for agent CLIs to be running in their panes.

        Polls `pane_current_command` until both panes transition away from
        their pre-launch baseline command (the shell). This avoids hardcoding
        shell names and handles Codex reporting as `node` instead of `codex`.

        Args:
            layout: Active pane layout.
            baseline: Pre-launch `pane_current_command` for each agent label.
        """
        deadline = time.time() + 30
        agents = [
            ("codex", layout.codex),
            ("claude", layout.claude),
        ]
        waiting = {label for label, _ in agents}
        pending = ", ".join(sorted(waiting))
        self._write_status_line(self._status_line("agents", pending))

        while waiting:
            if time.time() > deadline:
                self._clear_terminal_line()
                for label, pane_id in agents:
                    if label in waiting:
                        cmd = pane_current_command(pane_id)
                        print(
                            f"    [!!] {label} pane {pane_id}: "
                            f"current_command={cmd!r} baseline={baseline.get(label)!r}"
                        )
                pending = ", ".join(sorted(waiting))
                raise ClaodexError(
                    f"agent startup timeout: {pending} never appeared in pane; "
                    "check tmux panes for errors"
                )

            for label, pane_id in agents:
                if label not in waiting:
                    continue
                if not is_pane_alive(pane_id):
                    raise ClaodexError(
                        f"{label} pane died during startup; "
                        f"check tmux pane {pane_id} for errors"
                    )
                cmd = pane_current_command(pane_id)
                if cmd and cmd != baseline.get(label):
                    waiting.discard(label)
                    if waiting:
                        pending = ", ".join(sorted(waiting))
                        self._write_status_line(self._status_line("agents", pending))

            if waiting:
                time.sleep(1.0)
        self._clear_terminal_line()

    def _clear_session_state(self, workspace_root: Path) -> None:
        """Remove stale participant and cursor files from a prior session.

        Called on fresh start to prevent registration wait from accepting
        leftover entries and to force cursor reinitialization.

        Args:
            workspace_root: Workspace root path.
        """
        for agent in AGENTS:
            path = participant_file(workspace_root, agent)
            if path.exists():
                path.unlink()
        for agent in AGENTS:
            path = read_cursor_file(workspace_root, agent)
            if path.exists():
                path.unlink()
        for target in AGENTS:
            path = delivery_cursor_file(workspace_root, target)
            if path.exists():
                path.unlink()
        clear_ui_state_files(workspace_root)

    def _wait_for_registration(self, workspace_root: Path) -> SessionParticipants:
        """Wait for both agents to complete registration.

        Skill triggers are prefilled in each agent pane; the user presses
        Enter to invoke them. register.py writes a participant JSON file.
        This method polls for those files.

        Uses _rewrite_status_block to redraw the full status block on each
        update, which handles stray newlines from accidental keypresses.

        Args:
            workspace_root: Workspace root path.

        Returns:
            Loaded participant metadata.
        """
        # generous timeout: agent CLIs and skill registration can take time
        deadline = time.time() + 300
        waiting = {"claude", "codex"}
        done: list[str] = []
        self._rewrite_status_block(done, waiting)

        while waiting:
            if time.time() > deadline:
                self._clear_terminal_line()
                pending = ", ".join(sorted(waiting))
                raise ClaodexError(f"registration timeout waiting for: {pending}")

            for agent in list(waiting):
                path = participant_file(workspace_root, agent)
                if not path.exists():
                    continue
                try:
                    load_participant(workspace_root, agent)
                except ClaodexError:
                    continue
                waiting.remove(agent)
                done.append(agent)
                self._rewrite_status_block(done, waiting)

            if waiting:
                time.sleep(1.0)

        return load_participants(workspace_root)

    @staticmethod
    def _home_shorthand(path: Path) -> str:
        """Replace $HOME prefix with ~ for display."""
        home = str(Path.home())
        s = str(path)
        # ensure match is at a path boundary, not a string prefix
        # e.g. /home/master should match but /home/mastering should not
        if s == home:
            return "~"
        if s.startswith(home + "/"):
            return "~" + s[len(home):]
        return s

    def _install_skill_assets(self) -> None:
        """Install/update claodex skill into claude/codex home skill dirs."""
        source = Path(__file__).resolve().parent / "skill"
        if not source.exists():
            raise ClaodexError(f"skill source missing: {source}")

        # files that must exist after a successful copy
        required_files = ("SKILL.md", "scripts/register.py")

        claude_root = Path(
            os.getenv("CLAODEX_CLAUDE_SKILLS_DIR", str(Path.home() / ".claude" / "skills"))
        )
        codex_root = Path(
            os.getenv("CLAODEX_CODEX_SKILLS_DIR", str(Path.home() / ".codex" / "skills"))
        )

        targets = [root / "claodex" for root in (claude_root, codex_root)]
        print(f"  installing skills from:")
        print(f"    {self._home_shorthand(source)}")
        for target in targets:
            print(f"    -> {self._home_shorthand(target)}")

        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            # skip __init__.py and __pycache__ — they exist for setuptools
            # package discovery but are not needed in the installed skill
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__init__.py", "__pycache__"),
            )

            # verify every required file landed
            missing = [f for f in required_files if not (target / f).exists()]
            if missing:
                raise ClaodexError(
                    f"skill install to {target} incomplete, missing: {', '.join(missing)}"
                )

    def _run_repl(self, workspace_root: Path, participants: SessionParticipants) -> None:
        """Run the interactive command loop.

        Args:
            workspace_root: Workspace root path.
            participants: Active participants.
        """
        config = RoutingConfig(
            poll_seconds=float(os.getenv("CLAODEX_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))),
            turn_timeout_seconds=int(
                os.getenv("CLAODEX_TURN_TIMEOUT_SECONDS", str(DEFAULT_TURN_TIMEOUT_SECONDS))
            ),
        )
        target = "claude"
        bus = UIEventBus(workspace_root=workspace_root, default_target=target)
        try:
            router = Router(
                workspace_root=workspace_root,
                participants=participants,
                paste_content=paste_content,
                pane_alive=is_pane_alive,
                config=config,
                warning_callback=lambda warning: self._log_event(bus, "error", warning),
            )

            idle_callback = self._make_idle_callback(router, bus=bus)
            self._log_event(bus, "system", "claodex ready")

            while True:
                try:
                    event = self._read_event(target, on_idle=idle_callback)
                except KeyboardInterrupt:
                    self._log_event(bus, "system", "keyboard interrupt")
                    continue

                if event.kind == "quit":
                    self._log_event(bus, "system", "shutting down")
                    kill_session(self._session_name)
                    self._log_event(bus, "system", "session killed")
                    return

                if event.kind == "toggle":
                    target = peer_agent(target)
                    self._input_prefill = event.value
                    self._update_metrics(bus, target=target)
                    continue

                if event.kind == "collab_initiated" and self._collab_seed is not None:
                    seed_pending, seed_response = self._collab_seed
                    self._collab_seed = None
                    # preserve in-progress user input for after collab
                    if event.value:
                        self._input_prefill = event.value
                    self._clear_watches(router)
                    peer = peer_agent(seed_response.agent)
                    request = CollabRequest(
                        turns=DEFAULT_COLLAB_TURNS,
                        start_agent=peer,
                        message="",
                    )
                    self._log_event(
                        bus,
                        "collab",
                        f"{seed_response.agent} initiated collaboration",
                        agent=seed_response.agent,
                    )
                    self._clear_terminal_line()
                    self._run_collab(
                        workspace_root,
                        router,
                        request,
                        seed_turn=(seed_pending, seed_response),
                        bus=bus,
                    )
                    self._clear_terminal_line()
                    continue

                if event.kind != "submit":
                    continue

                try:
                    text = event.value.strip()
                    if not text:
                        continue

                    if text.startswith("/"):
                        if text == "/quit":
                            self._log_event(bus, "system", "shutting down")
                            kill_session(self._session_name)
                            self._log_event(bus, "system", "session killed")
                            return
                        if text == "/status":
                            self._emit_status(
                                workspace_root=workspace_root,
                                participants=router.participants,
                                target=target,
                                bus=bus,
                            )
                            continue
                        if text == "/halt":
                            self._log_event(bus, "system", "no active collaboration to halt")
                            continue
                        if text.startswith("/collab"):
                            request = parse_collab_request(text, default_start=target)
                            self._clear_watches(router)
                            self._clear_terminal_line()
                            self._run_collab(
                                workspace_root,
                                router,
                                request,
                                bus=bus,
                            )
                            self._clear_terminal_line()
                            continue
                        self._log_event(bus, "error", f"unknown command: {text}")
                        continue

                    if self._post_halt:
                        text = f"(collab halted by user)\n\n{text}"
                        self._post_halt = False

                    pending = router.send_user_message(target, text)
                    self._log_event(bus, "sent", f"-> {target}", target=target)
                    self._mark_agent_thinking(bus, target, sent_at=pending.sent_at)

                    # track for idle [COLLAB] detection; clean up any prior
                    # watch latch if there was already one for this target
                    if target in self._pending_watches:
                        old = self._pending_watches[target]
                        router.clear_poll_latch(target, old.before_cursor)
                        # preserve prior pending blocks for seeded exchange logs
                        if old.blocks:
                            pending.blocks = [*old.blocks, *pending.blocks]
                            if old.sent_at is not None and (
                                pending.sent_at is None or old.sent_at < pending.sent_at
                            ):
                                pending.sent_at = old.sent_at
                    self._pending_watches[target] = pending
                except ClaodexError as exc:
                    self._log_event(bus, "error", str(exc))
                    continue
        finally:
            bus.close()

    def _clear_watches(self, router: Router) -> None:
        """Clear all pending watches and their poll latches."""
        for agent, pending in self._pending_watches.items():
            router.clear_poll_latch(agent, pending.before_cursor)
        self._pending_watches.clear()

    def _make_idle_callback(
        self,
        router: Router,
        bus: UIEventBus | None = None,
    ) -> "Callable[[], InputEvent | None]":
        """Build an idle callback that polls pending watches for [COLLAB].

        Args:
            router: Active message router.

        Returns:
            Callback suitable for InputEditor.read(on_idle=...).
        """
        from .input_editor import InputEvent

        def _poll() -> InputEvent | None:
            # detect session file changes from agent re-registration
            self._check_for_reregistration(router.workspace_root, router, bus)

            expired = []
            # iterate a snapshot to allow mutation during the loop
            for agent, pending in list(self._pending_watches.items()):
                # check for watch timeout
                if pending.sent_at is not None:
                    elapsed = (datetime.now(timezone.utc) - pending.sent_at).total_seconds()
                    if elapsed > router.config.turn_timeout_seconds:
                        expired.append(agent)
                        continue

                try:
                    response = router.poll_for_response(pending)
                except ClaodexError as exc:
                    del self._pending_watches[agent]
                    router.clear_poll_latch(agent, pending.before_cursor)
                    self._log_event(bus, "watch", f"error polling {agent}: {exc}", agent=agent)
                    return None

                if response is None:
                    continue

                # agent responded — remove watch regardless of signal
                del self._pending_watches[agent]
                words = count_words(response.text)
                latency = self._response_latency_seconds(pending)
                self._mark_agent_idle(
                    bus,
                    response.agent,
                    words=words,
                    latency_seconds=latency,
                )
                self._log_event(
                    bus,
                    "recv",
                    f"<- {response.agent} ({words} words)",
                    agent=response.agent,
                )

                if _last_line_is(response.text, COLLAB_SIGNAL):
                    # keep the signal in routed text; only use stripped text
                    # to detect empty signal-only responses
                    stripped_text = _strip_trailing_signal(response.text, COLLAB_SIGNAL)
                    if not stripped_text.strip():
                        self._log_event(
                            bus,
                            "watch",
                            f"{agent} signaled [COLLAB] with no content, ignoring",
                            agent=agent,
                        )
                        return None
                    seeded_response = ResponseTurn(
                        agent=response.agent,
                        text=response.text,
                        source_cursor=response.source_cursor,
                        received_at=response.received_at,
                    )
                    # stash the seed turn for the REPL to pick up
                    self._collab_seed = (pending, seeded_response)
                    return InputEvent(kind="collab_initiated")

                return None

            for agent in expired:
                pending = self._pending_watches.pop(agent)
                router.clear_poll_latch(agent, pending.before_cursor)
                self._log_event(bus, "watch", f"expired collab watch for {agent}", agent=agent)

            return None

        return _poll

    def _read_event(self, target: str, on_idle: "Callable[[], InputEvent | None] | None" = None):
        """Read one REPL event.

        Args:
            target: Current target label.
            on_idle: Optional idle callback for the input editor.

        Returns:
            Input event from editor or fallback input mode.
        """
        if sys.stdin.isatty() and sys.stdout.isatty():
            prefill = self._input_prefill
            self._input_prefill = ""
            return self._editor.read(target, on_idle=on_idle, prefill=prefill)

        try:
            line = input(f"{target} > ")
        except EOFError:
            from .input_editor import InputEvent

            return InputEvent(kind="quit")
        from .input_editor import InputEvent

        return InputEvent(kind="submit", value=line)

    # ansi color constants for registration status display
    _ANSI_RESET = "\033[0m"
    _ANSI_DIM = "\033[90m"
    _ANSI_GREEN = "\033[32m"
    _ANSI_CLAUDE = "\033[38;5;216m"  # orange/salmon — matches prompt
    _ANSI_CODEX = "\033[38;5;116m"  # teal — matches prompt
    _AGENT_COLORS = {"claude": _ANSI_CLAUDE, "codex": _ANSI_CODEX}

    @staticmethod
    def _status_line(
        label: str, status: str, indent: int = 2, color: bool = False
    ) -> str:
        """Format a dot-leader status line.

        Produces lines like: '  skill assets .............. ok'

        Width adapts to terminal size, clamped so narrow terminals
        don't wrap and wide terminals don't stretch absurdly.

        Args:
            label: Step name (left side).
            status: Status text (right side).
            indent: Leading space count.
            color: Whether to apply ANSI colors (agent name color,
                   dim dots, green/dim status).

        Returns:
            Formatted line string (no trailing newline).
        """
        columns = shutil.get_terminal_size().columns
        # usable width: never exceed available columns, cap upper bound at 40
        available = columns - indent - 1
        usable = min(available, 40)
        # space for label + one space + one dot minimum + one space + status
        dots_budget = usable - len(label) - len(status) - 2
        if dots_budget < 1:
            # terminal too narrow for dots — just space-separate
            if color:
                return ClaodexApplication._colorize_status_line(
                    indent, label, " ", status
                )
            return f"{' ' * indent}{label} {status}"
        dots = "." * dots_budget
        if color:
            return ClaodexApplication._colorize_status_line(
                indent, label, f" {dots} ", status
            )
        return f"{' ' * indent}{label} {dots} {status}"

    @staticmethod
    def _colorize_status_line(
        indent: int, label: str, separator: str, status: str
    ) -> str:
        """Apply ANSI colors to a status line.

        Agent names get their prompt color; dots are dim; 'ok' is green;
        'waiting' and other statuses are dim.

        Args:
            indent: Leading space count.
            label: Step name.
            separator: Dot leader or space between label and status.
            status: Status text.

        Returns:
            ANSI-colored line string.
        """
        r = ClaodexApplication._ANSI_RESET
        dim = ClaodexApplication._ANSI_DIM
        # color agent names with their prompt color
        agent_color = ClaodexApplication._AGENT_COLORS.get(label)
        if agent_color:
            colored_label = f"{agent_color}{label}{r}"
        else:
            colored_label = label
        # color the status word
        if status == "ok":
            colored_status = f"{ClaodexApplication._ANSI_GREEN}{status}{r}"
        else:
            colored_status = f"{dim}{status}{r}"
        colored_sep = f"{dim}{separator}{r}"
        return f"{' ' * indent}{colored_label}{colored_sep}{colored_status}"

    @staticmethod
    def _clear_terminal_line() -> None:
        """Clear the active terminal line in TTY mode."""
        if not sys.stdout.isatty():
            return
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    @staticmethod
    def _write_status_line(message: str) -> None:
        """Write a transient status line, overwriting the current line in TTY mode."""
        if not sys.stdout.isatty():
            print(message)
            return
        sys.stdout.write(f"\r\033[K{message}")
        sys.stdout.flush()

    @staticmethod
    def _finish_status_line() -> None:
        """Finalize a transient status line with newline in TTY mode."""
        if not sys.stdout.isatty():
            return
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _rewrite_status_block(self, done: list[str], waiting: set[str]) -> None:
        """Clear screen and redraw registration progress.

        Redraws from scratch on each update so stray newlines from
        accidental keypresses don't leave orphaned status lines.
        The screen is wiped after registration completes anyway.

        Iterates agents in deterministic order (claude, codex)
        regardless of registration arrival order.
        """
        # registration runs in the input pane (TTY), so color=True
        is_tty = sys.stdout.isatty()
        if not is_tty:
            # non-TTY: just print incrementally, no colors
            if done:
                print(self._status_line(done[-1], "ok"))
            if waiting:
                for agent in AGENTS:
                    if agent in waiting:
                        print(self._status_line(agent, "waiting"))
            return
        # clear screen and redraw all lines including the prompt,
        # so accidental Enter presses don't orphan any lines
        dim = self._ANSI_DIM
        reset = self._ANSI_RESET
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.write(
            f"\n  {dim}press Enter in each agent pane to invoke the skill and register{reset}\n"
        )
        sys.stdout.write("\n")
        done_set = set(done)
        for agent in AGENTS:
            if agent in done_set:
                sys.stdout.write(self._status_line(agent, "ok", color=True) + "\n")
            elif agent in waiting:
                sys.stdout.write(self._status_line(agent, "waiting", color=True) + "\n")
        sys.stdout.flush()

    @staticmethod
    def _clear_terminal_screen() -> None:
        """Clear the active terminal screen in TTY mode."""
        if not sys.stdout.isatty():
            return
        sys.stdout.write("\033[2J\033[H\033[3J")
        sys.stdout.flush()

    def _run_collab(
        self,
        workspace_root: Path,
        router: Router,
        request: CollabRequest,
        *,
        seed_turn: tuple[PendingSend, ResponseTurn] | None = None,
        bus: UIEventBus | None = None,
    ) -> None:
        """Run multi-turn automated collaboration.

        Args:
            workspace_root: Project root.
            router: Active message router.
            request: Parsed collaboration request.
            seed_turn: Optional first turn for agent-initiated collab.
                When provided the agent's response is routed to the peer
                as turn 1 and the initial user-message send is skipped.
        """
        self._log_event(
            bus,
            "collab",
            f"starting: target={request.start_agent} turns={request.turns}",
            target=request.start_agent,
        )
        self._update_metrics(bus, mode="collab", collab_turn=None, collab_max=request.turns)
        started_at = datetime.now().astimezone().replace(microsecond=0)
        initiated_by = seed_turn[1].agent if seed_turn else "user"
        exchange_path, exchange_handle = self._open_exchange_log(
            workspace_root=workspace_root,
            initial_message=request.message,
            started_at=started_at,
            initiated_by=initiated_by,
        )
        exchange_first_message = True

        stop_reason = "turns_reached"
        turns_completed = 0
        pending: PendingSend | None = None
        pending_is_routed = False
        pending_replay_interjections: list[str] = []
        last_active_target = request.start_agent

        turn_records: list[tuple[PendingSend, ResponseTurn]] = []
        # tracks a response that was received but not yet routed onward
        last_unrouted_response_agent: str | None = None
        # prevent stale interjections from a previous collab from leaking
        # into this run
        _drain_queue(self._collab_interjections)

        halt_event = threading.Event()
        stop_listener = threading.Event()
        listener_kwargs: dict[str, object] = {
            "halt_event": halt_event,
            "stop_event": stop_listener,
            "editor": self._editor,
        }
        if bus is not None:
            listener_kwargs["bus"] = bus
        listener = threading.Thread(
            target=self._halt_listener,
            kwargs=listener_kwargs,
            daemon=True,
        )
        listener.start()

        try:
            if seed_turn:
                # agent-initiated: first turn already completed, route to peer
                seed_pending, seed_response = seed_turn
                turns_completed = 1
                turn_records.append(seed_turn)
                for source, body in seed_pending.blocks:
                    exchange_first_message = self._append_exchange_message(
                        exchange_handle,
                        source,
                        body,
                        seed_pending.sent_at,
                        first_message=exchange_first_message,
                    )
                exchange_first_message = self._append_exchange_message(
                    exchange_handle,
                    seed_response.agent,
                    seed_response.text,
                    seed_response.received_at,
                    first_message=exchange_first_message,
                )
                words = count_words(seed_response.text)
                self._mark_agent_idle(
                    bus,
                    seed_response.agent,
                    words=words,
                    latency_seconds=self._response_latency_seconds(seed_pending),
                )
                self._update_metrics(bus, collab_turn=turns_completed, collab_max=request.turns)
                self._log_event(
                    bus,
                    "recv",
                    f"<- {seed_response.agent} ({words} words)",
                    agent=seed_response.agent,
                )
                self._log_event(
                    bus,
                    "collab",
                    f"turn 1 <- {seed_response.agent} ({words} words)",
                    agent=seed_response.agent,
                )
                next_target = peer_agent(seed_response.agent)
                pending = router.send_routed_message(
                    target_agent=next_target,
                    source_agent=seed_response.agent,
                    response_text=seed_response.text,
                )
                self._log_event(bus, "sent", f"-> {next_target}", target=next_target)
                pending_is_routed = True
                last_active_target = next_target
                self._mark_agent_thinking(bus, next_target, sent_at=pending.sent_at)
                self._log_event(bus, "collab", f"routing -> {next_target}", target=next_target)
            else:
                pending = router.send_user_message(request.start_agent, request.message)
                self._log_event(
                    bus,
                    "sent",
                    f"-> {pending.target_agent}",
                    target=pending.target_agent,
                )
                pending_is_routed = False
                last_active_target = pending.target_agent
                self._mark_agent_thinking(bus, pending.target_agent, sent_at=pending.sent_at)
                for source, body in pending.blocks:
                    exchange_first_message = self._append_exchange_message(
                        exchange_handle,
                        source,
                        body,
                        pending.sent_at,
                        first_message=exchange_first_message,
                    )

            while turns_completed < request.turns:
                self._log_event(
                    bus,
                    "collab",
                    f"turn {turns_completed + 1} -> {pending.target_agent} (waiting...)",
                    target=pending.target_agent,
                )
                try:
                    response = router.wait_for_response(pending)
                except KeyboardInterrupt:
                    halt_event.set()
                    stop_reason = "user_halt"
                    break

                turns_completed += 1
                words = count_words(response.text)
                self._mark_agent_idle(
                    bus,
                    response.agent,
                    words=words,
                    latency_seconds=self._response_latency_seconds(pending),
                )
                self._update_metrics(bus, collab_turn=turns_completed, collab_max=request.turns)
                self._log_event(
                    bus,
                    "recv",
                    f"<- {response.agent} ({words} words)",
                    agent=response.agent,
                )
                self._log_event(
                    bus,
                    "collab",
                    f"turn {turns_completed} <- {response.agent} ({words} words)",
                    agent=response.agent,
                )

                turn_records.append((pending, response))
                exchange_first_message = self._append_exchange_message(
                    exchange_handle,
                    response.agent,
                    response.text,
                    response.received_at,
                    first_message=exchange_first_message,
                )
                last_unrouted_response_agent = response.agent

                # convergence: both agents signaled in consecutive turns
                if (
                    len(turn_records) >= 2
                    and _last_line_is(turn_records[-1][1].text, CONVERGE_SIGNAL)
                    and _last_line_is(turn_records[-2][1].text, CONVERGE_SIGNAL)
                ):
                    stop_reason = "converged"
                    break

                if halt_event.is_set():
                    stop_reason = "user_halt"
                    break

                if turns_completed >= request.turns:
                    stop_reason = "turns_reached"
                    break

                # include fresh user interjections from this turn and replay
                # prior-turn interjections so both agents eventually receive
                # them
                queued_interjections = _drain_queue(self._collab_interjections)
                fresh_interjections = [text.strip() for text in queued_interjections if text.strip()]
                routed_interjections = [*pending_replay_interjections, *fresh_interjections]

                next_target = peer_agent(response.agent)
                echoed_user_anchor = pending.sent_text if pending_is_routed else None
                pending = router.send_routed_message(
                    target_agent=next_target,
                    source_agent=response.agent,
                    response_text=response.text,
                    user_interjections=routed_interjections or None,
                    echoed_user_anchor=echoed_user_anchor,
                )
                last_unrouted_response_agent = None
                self._log_event(bus, "sent", f"-> {next_target}", target=next_target)
                pending_is_routed = True
                pending_replay_interjections = fresh_interjections
                last_active_target = next_target
                self._mark_agent_thinking(bus, next_target, sent_at=pending.sent_at)
                if fresh_interjections:
                    for body in fresh_interjections:
                        exchange_first_message = self._append_exchange_message(
                            exchange_handle,
                            "user",
                            body,
                            pending.sent_at,
                            first_message=exchange_first_message,
                        )
                    self._log_event(
                        bus,
                        "collab",
                        f"routing -> {next_target} (with {len(fresh_interjections)} user interjection(s))",
                        target=next_target,
                    )
                else:
                    self._log_event(bus, "collab", f"routing -> {next_target}", target=next_target)

        except ClaodexError as exc:
            stop_reason = str(exc)
            self._log_event(bus, "collab", f"halted: {exc}")
            self._log_event(bus, "error", str(exc))
        except KeyboardInterrupt:
            stop_reason = "user_halt"
            self._log_event(bus, "collab", "halted by user")
        finally:
            stop_listener.set()
            listener.join(timeout=0.5)
            try:
                sync_targets: list[str] | None = None
                if last_unrouted_response_agent is not None:
                    # a completed response was received but not routed onward;
                    # preserve it for the peer's next normal-mode delivery
                    unsynced_target = peer_agent(last_unrouted_response_agent)
                    sync_targets = [agent for agent in AGENTS if agent != unsynced_target]
                router.sync_delivery_cursors(sync_targets)
            except ClaodexError as exc:
                self._log_event(bus, "error", f"delivery cursor sync failed: {exc}")
            self._close_exchange_log(exchange_handle, turns_completed, stop_reason)

        # any queued interjections that were not routed inline are dropped
        # when collab stops
        remaining = _drain_queue(self._collab_interjections)
        if remaining:
            self._log_event(
                bus,
                "collab",
                f"dropped {len(remaining)} queued interjection(s)",
            )

        self._log_event(
            bus,
            "collab",
            f"halted: {turns_completed} turns, reason={stop_reason}, exchange={exchange_path}",
        )
        self._log_event(
            bus,
            "collab",
            f"last active target: {last_active_target}",
            target=last_active_target,
        )
        if stop_reason == "user_halt":
            self._post_halt = True
        self._update_metrics(
            bus,
            mode="normal",
            collab_turn=None,
            collab_max=None,
        )

    def _halt_listener(
        self,
        halt_event: threading.Event,
        stop_event: threading.Event,
        editor: InputEditor | None = None,
        bus: UIEventBus | None = None,
    ) -> None:
        """Watch stdin for `/halt` while collab mode runs.

        Args:
            halt_event: Set when halt is requested.
            stop_event: Set when listener should stop.
            editor: Shared input editor to preserve prompt history across modes.
            bus: Optional event bus for collab status events.
        """
        if not sys.stdin.isatty():
            return

        from .input_editor import InputEvent

        shared_editor = editor or self._editor

        def _on_idle() -> InputEvent | None:
            if stop_event.is_set() or halt_event.is_set():
                return InputEvent(kind="quit")
            return None

        while not stop_event.is_set() and not halt_event.is_set():
            try:
                event = shared_editor.read("collab", on_idle=_on_idle)
            except KeyboardInterrupt:
                dropped = len(_drain_queue(self._collab_interjections))
                halt_event.set()
                if dropped:
                    self._log_event(
                        bus,
                        "collab",
                        f"halt requested (dropped {dropped} queued interjection(s))",
                    )
                else:
                    self._log_event(bus, "collab", "halt requested")
                break

            if event.kind == "quit":
                continue
            if event.kind != "submit":
                continue

            stripped = event.value.strip()
            if stripped == "/halt":
                dropped = len(_drain_queue(self._collab_interjections))
                halt_event.set()
                if dropped:
                    self._log_event(
                        bus,
                        "collab",
                        f"halt requested (dropped {dropped} queued interjection(s))",
                    )
                else:
                    self._log_event(bus, "collab", "halt requested")
            elif stripped:
                # queue for inclusion in the next routed message;
                # collab keeps flowing
                self._collab_interjections.put(stripped)
                self._log_event(bus, "collab", "interjection queued")

    def _response_latency_seconds(self, pending: PendingSend) -> float | None:
        """Return latency in seconds from send metadata."""
        if pending.sent_at is None:
            return None
        delta = datetime.now(timezone.utc) - pending.sent_at
        return max(0.0, delta.total_seconds())

    def _mark_agent_thinking(
        self,
        bus: UIEventBus | None,
        agent: str,
        *,
        sent_at: datetime | None = None,
    ) -> None:
        """Set one agent to thinking in metrics."""
        if bus is None:
            return
        thinking_since = (sent_at or datetime.now(timezone.utc)).isoformat()
        bus.update_metrics(
            agents={
                agent: {
                    "status": "thinking",
                    "thinking_since": thinking_since,
                }
            }
        )

    def _mark_agent_idle(
        self,
        bus: UIEventBus | None,
        agent: str,
        *,
        words: int | None = None,
        latency_seconds: float | None = None,
    ) -> None:
        """Set one agent to idle in metrics and record optional turn stats."""
        if bus is None:
            return
        update: dict[str, object] = {
            "status": "idle",
            "thinking_since": None,
        }
        if words is not None:
            update["last_words"] = words
        update["last_latency_s"] = latency_seconds
        bus.update_metrics(agents={agent: update})

    def _update_metrics(self, bus: UIEventBus | None, **fields: object) -> None:
        """Apply metrics updates when a bus is available."""
        if bus is None:
            return
        bus.update_metrics(**fields)

    def _log_event(
        self,
        bus: UIEventBus | None,
        kind: str,
        message: str,
        *,
        agent: str | None = None,
        target: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> None:
        """Emit one UI event when a bus is available."""
        if bus is None:
            return
        bus.log(kind, message, agent=agent, target=target, meta=meta)

    def _open_exchange_log(
        self,
        workspace_root: Path,
        initial_message: str,
        started_at: datetime,
        initiated_by: str = "user",
    ) -> tuple[Path, TextIO]:
        """Create the exchange log file and write the static header.

        Args:
            workspace_root: Workspace root for state output.
            initial_message: User collab prompt.
            started_at: Collab start timestamp.
            initiated_by: Who started the collab ("user" or agent name).

        Returns:
            Open file path and writable text handle.
        """
        timestamp = started_at.strftime("%y%m%d-%H%M%S")
        output_dir = exchanges_dir(workspace_root)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{timestamp}.md"

        header_message = initial_message.strip().replace("\n", " ")[:80]
        handle = output_path.open("w", encoding="utf-8")
        lines = (
            f"# Collaboration: {header_message}",
            "",
            f"Started: {started_at.isoformat()}",
            f"Initiated by: {initiated_by}",
            "Agents: claude ↔ codex",
            "",
        )
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        return output_path, handle

    def _append_exchange_message(
        self,
        handle: TextIO,
        source: str,
        body: str,
        timestamp: datetime | None,
        *,
        first_message: bool,
    ) -> bool:
        """Append one source message to an open exchange log and flush.

        Args:
            handle: Open markdown file handle.
            source: Message source label.
            body: Message body text.
            timestamp: Timestamp for display.
            first_message: Whether this is the first log message.

        Returns:
            False after append so callers can keep a simple first-message flag.
        """
        if not first_message:
            handle.write("---\n\n")
        ts_str = f" · {_format_local_time(timestamp)}" if timestamp else ""
        cleaned_body = _strip_routing_signals(body)
        handle.write(f"## {source}{ts_str}\n")
        handle.write(f"{cleaned_body}\n\n")
        handle.flush()
        return False

    def _close_exchange_log(self, handle: TextIO, turns: int, stop_reason: str) -> None:
        """Write exchange summary footer and close the file handle."""
        handle.write("---\n\n")
        handle.write(f"*Turns: {turns} · Stop reason: {stop_reason}*\n")
        handle.flush()
        handle.close()

    def _emit_status(
        self,
        workspace_root: Path,
        participants: SessionParticipants,
        target: str,
        bus: UIEventBus | None,
    ) -> None:
        """Emit readable runtime status to the sidebar event log."""
        snapshot = cursor_snapshot(workspace_root)
        watches = sorted(self._pending_watches.keys())
        seed_agent = self._collab_seed[1].agent if self._collab_seed else None

        # build human-readable status lines
        lines = [
            f"target: {target}",
            f"claude: pane={participants.claude.tmux_pane}"
            f"  session={participants.claude.session_id}",
            f"codex:  pane={participants.codex.tmux_pane}"
            f"  session={participants.codex.session_id}",
        ]
        for name, value in sorted(snapshot.items()):
            lines.append(f"  {name}: {value}")
        if watches:
            lines.append(f"watches: {', '.join(watches)}")
        if seed_agent:
            lines.append(f"collab seed: {seed_agent}")

        self._log_event(
            bus,
            "status",
            "\n".join(lines),
            target=target,
        )

    @staticmethod
    def _print_help() -> None:
        """Print command usage."""
        print("usage:")
        print("  python3 -m claodex [directory]")
        print("  python3 -m claodex attach [directory]")
        print("  python3 -m claodex sidebar [directory]")


def parse_collab_request(command_text: str, default_start: str) -> CollabRequest:
    """Parse `/collab` command arguments.

    Args:
        command_text: Raw command text.
        default_start: Default start agent when option omitted.

    Returns:
        Parsed collaboration request.
    """
    # split on whitespace — no shell quoting rules, so apostrophes
    # and other punctuation in the message body work naturally
    pieces = command_text.split()
    if not pieces or pieces[0] != "/collab":
        raise ClaodexError("validation error: malformed collab command")

    turns = DEFAULT_COLLAB_TURNS
    start_agent = default_start

    index = 1
    while index < len(pieces):
        token = pieces[index]
        if token == "--turns":
            if index + 1 >= len(pieces):
                raise ClaodexError("validation error: --turns requires a value")
            try:
                turns = int(pieces[index + 1])
            except ValueError as exc:
                raise ClaodexError("validation error: --turns must be an integer") from exc
            if turns <= 0:
                raise ClaodexError("validation error: --turns must be positive")
            index += 2
            continue

        if token == "--start":
            if index + 1 >= len(pieces):
                raise ClaodexError("validation error: --start requires a value")
            candidate = pieces[index + 1]
            if candidate not in AGENTS:
                raise ClaodexError("validation error: --start must be 'claude' or 'codex'")
            start_agent = candidate
            index += 2
            continue

        # explicit end-of-options marker
        if token == "--":
            index += 1
            break
        # reject unrecognized options before they silently become message text
        if token.startswith("--"):
            raise ClaodexError(f"validation error: unknown option '{token}'")
        break

    # extract the message as the raw remainder after consuming option tokens,
    # using maxsplit to preserve original spacing and punctuation
    parts = command_text.split(maxsplit=index)
    message = parts[index].strip() if len(parts) > index else ""
    if not message:
        raise ClaodexError("validation error: /collab requires a message")

    return CollabRequest(turns=turns, start_agent=start_agent, message=message)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint.

    Args:
        argv: Optional argv vector.

    Returns:
        Exit status code.
    """
    application = ClaodexApplication()
    return application.run(argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())

"""claodex CLI entrypoint and interactive loop."""

from __future__ import annotations

import os
import select
import shlex
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .constants import (
    AGENTS,
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
    cursor_snapshot,
    delivery_cursor_file,
    ensure_gitignore_entry,
    ensure_state_layout,
    exchanges_dir,
    initialize_cursors_from_line_counts,
    load_participant,
    load_participants,
    participant_file,
    peer_agent,
    read_cursor_file,
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
)


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

        workspace_root = self._resolve_workspace(Path(directory_arg))

        try:
            if mode == "start":
                return self._run_start(workspace_root)
            return self._run_attach(workspace_root)
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
        workspace_root = resolve_workspace_root(candidate)
        if not (workspace_root / ".git").exists() and not (workspace_root / ".claodex").exists():
            raise ClaodexError(
                "workspace must be a git repository or already contain .claodex state"
            )
        return workspace_root

    def _run_start(self, workspace_root: Path) -> int:
        """Run startup flow then attach to tmux.

        Startup sequence:
        1. clear stale participant files
        2. create tmux session with 3 panes
        3. launch agent processes (codex, claude)
        4. wait for agents to be ready (pane command transition)
        5. prepopulate skill commands in each pane
        6. paste REPL command into CLI pane
        7. attach to tmux — user presses Enter in agent panes to register
        8. REPL (attach mode) waits for registration, binds layout, inits cursors

        Args:
            workspace_root: Workspace root path.

        Returns:
            Exit status code.
        """
        ensure_dependencies()
        if session_exists(SESSION_NAME):
            raise ClaodexError(
                "tmux session 'claodex' already exists; run 'claodex attach' or kill the session"
            )

        ensure_state_layout(workspace_root)
        ensure_gitignore_entry(workspace_root)
        self._install_skill_assets()

        # clear all stale state from prior sessions: participant files (so
        # registration wait won't accept leftovers) and cursor files (so
        # attach mode will reinitialize rather than reuse stale positions)
        self._clear_session_state(workspace_root)

        created_session = False
        try:
            print("creating tmux session...")
            layout = create_session(workspace_root, session_name=SESSION_NAME)
            created_session = True
            print(f"  panes: codex={layout.codex}  claude={layout.claude}  cli={layout.cli}")

            # capture baseline shell commands before launching agents
            baseline = {
                "codex": pane_current_command(layout.codex),
                "claude": pane_current_command(layout.claude),
            }
            print("launching agent processes...")
            start_agent_processes(layout, workspace_root)
            self._wait_for_agents_ready(layout, baseline)

            # prepopulate skill triggers so user only needs to press Enter
            print("prefilling skill commands...")
            prefill_skill_commands(layout)

            # paste REPL into CLI pane and attach immediately so the user
            # can see the agent panes and press Enter to trigger registration
            attach_cli_pane(layout)
            exe = shlex.quote(sys.executable)
            ws = shlex.quote(str(workspace_root))
            repl_cmd = f"{exe} -m claodex attach {ws}"
            paste_content(layout.cli, repl_cmd)

            # hand the user's terminal to tmux
            if os.environ.get("TMUX"):
                os.execvp("tmux", ["tmux", "switch-client", "-t", SESSION_NAME])
            else:
                os.execvp("tmux", ["tmux", "attach-session", "-t", SESSION_NAME])
        except Exception:
            if created_session:
                kill_session(SESSION_NAME)
            raise

    def _run_attach(self, workspace_root: Path) -> int:
        """Attach to existing claodex tmux session.

        Handles two cases:
        - Fresh start: registration hasn't happened yet; wait for user to
          press Enter in agent panes, then init cursors.
        - Reattach: participants already registered; validate and resume.

        Args:
            workspace_root: Workspace root path.

        Returns:
            Exit status code.
        """
        ensure_dependencies()
        if not session_exists(SESSION_NAME):
            raise ClaodexError("tmux session 'claodex' does not exist")

        layout = resolve_layout(SESSION_NAME)
        participants = self._load_or_wait_participants(workspace_root)
        participants = self._bind_participants_to_layout(participants, layout)
        self._validate_registered_panes(participants)

        # init cursors on fresh start, validate on reattach
        if self._cursors_missing(workspace_root):
            initialize_cursors_from_line_counts(workspace_root, participants)
        else:
            self._ensure_cursor_files_exist(workspace_root)

        attach_cli_pane(layout)

        self._run_repl(workspace_root, participants)
        return 0

    def _load_or_wait_participants(self, workspace_root: Path) -> SessionParticipants:
        """Load participants, waiting for registration if needed.

        On fresh start the REPL launches before the user has pressed Enter
        in the agent panes. Only falls back to the polling wait when
        participant files are absent; malformed files raise immediately.

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

        print("waiting for agent registration (press Enter in each agent pane)...")
        return self._wait_for_registration(workspace_root)

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
        print("waiting for agent processes to start...")

        while waiting:
            if time.time() > deadline:
                for label, pane_id in agents:
                    if label in waiting:
                        cmd = pane_current_command(pane_id)
                        print(
                            f"  {label} pane {pane_id}: "
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
                    print(f"  {label} ready (process: {cmd})")
                    waiting.discard(label)

            if waiting:
                time.sleep(1.0)

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

    def _wait_for_registration(self, workspace_root: Path) -> SessionParticipants:
        """Wait for both agents to complete registration.

        The user presses Enter in each agent pane to trigger the prefilled
        skill command. The skill runs register.py which writes a participant
        JSON file. We poll for those files.

        Args:
            workspace_root: Workspace root path.

        Returns:
            Loaded participant metadata.
        """
        # generous timeout: user may need time to read the panes, press Enter
        deadline = time.time() + 300
        waiting = {"claude", "codex"}

        while waiting:
            if time.time() > deadline:
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
                print(f"  {agent} registered")
                waiting.remove(agent)

            if waiting:
                time.sleep(1.0)

        return load_participants(workspace_root)

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

        print("installing skill assets...")
        for root in (claude_root, codex_root):
            target = root / "claodex"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                print(f"  removing stale {target}")
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
            print(f"  installed {target}")

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
        router = Router(
            workspace_root=workspace_root,
            participants=participants,
            paste_content=paste_content,
            pane_alive=is_pane_alive,
            config=config,
        )

        target = "claude"
        print(
            "claodex ready | tab toggles target | ctrl+j newline | commands: /collab, /halt, /status, /quit"
        )

        while True:
            try:
                event = self._read_event(target)
            except KeyboardInterrupt:
                print("\nkeyboard interrupt")
                continue

            if event.kind == "quit":
                print("input closed")
                return

            if event.kind == "toggle":
                target = peer_agent(target)
                continue

            if event.kind != "submit":
                continue

            try:
                text = event.value.strip()
                if not text:
                    continue

                if text.startswith("/"):
                    if text == "/quit":
                        return
                    if text == "/status":
                        self._print_status(workspace_root, participants)
                        continue
                    if text == "/halt":
                        print("no active collaboration to halt")
                        continue
                    if text.startswith("/collab"):
                        request = parse_collab_request(text, default_start=target)
                        self._run_collab(workspace_root, router, request)
                        continue
                    print(f"unknown command: {text}")
                    continue

                router.send_user_message(target, text)
                print(f"[sent] -> {target}")
            except ClaodexError as exc:
                print(f"error: {exc}")
                continue

    def _read_event(self, target: str):
        """Read one REPL event.

        Args:
            target: Current target label.

        Returns:
            Input event from editor or fallback input mode.
        """
        if sys.stdin.isatty() and sys.stdout.isatty():
            return self._editor.read(target)

        try:
            line = input(f"{target} > ")
        except EOFError:
            from .input_editor import InputEvent

            return InputEvent(kind="quit")
        from .input_editor import InputEvent

        return InputEvent(kind="submit", value=line)

    def _run_collab(self, workspace_root: Path, router: Router, request: CollabRequest) -> None:
        """Run multi-turn automated collaboration.

        Args:
            router: Active message router.
            request: Parsed collaboration request.
        """
        print(
            f"[collab] starting: target={request.start_agent} turns={request.turns}"
        )
        started_at = datetime.now().astimezone().replace(microsecond=0)

        stop_reason = "turns_reached"
        turns_completed = 0
        pending: PendingSend | None = None
        last_active_target = request.start_agent

        turn_records: list[tuple[PendingSend, ResponseTurn]] = []

        halt_event = threading.Event()
        stop_listener = threading.Event()
        listener = threading.Thread(
            target=self._halt_listener,
            kwargs={"halt_event": halt_event, "stop_event": stop_listener},
            daemon=True,
        )
        listener.start()

        try:
            pending = router.send_user_message(request.start_agent, request.message)
            last_active_target = pending.target_agent

            while turns_completed < request.turns:
                print(f"[collab] turn {turns_completed + 1} -> {pending.target_agent} (waiting...)")
                try:
                    response = router.wait_for_response(pending)
                except KeyboardInterrupt:
                    halt_event.set()
                    stop_reason = "user_halt"
                    break

                turns_completed += 1
                words = count_words(response.text)
                print(
                    f"[collab] turn {turns_completed} <- {response.agent} ({words} words)"
                )

                turn_records.append((pending, response))

                if halt_event.is_set():
                    stop_reason = "user_halt"
                    break

                if turns_completed >= request.turns:
                    stop_reason = "turns_reached"
                    break

                next_target = peer_agent(response.agent)
                pending = router.send_routed_message(
                    target_agent=next_target,
                    source_agent=response.agent,
                    response_text=response.text,
                )
                last_active_target = next_target
                print(f"[collab] routing -> {next_target}")

        except ClaodexError as exc:
            stop_reason = str(exc)
            print(f"[collab] halted: {exc}")
        except KeyboardInterrupt:
            stop_reason = "user_halt"
            print("[collab] halted by user")
        finally:
            stop_listener.set()
            listener.join(timeout=0.5)

        exchange_path = self._write_exchange_log(
            workspace_root=workspace_root,
            turn_records=turn_records,
            initial_message=request.message,
            started_at=started_at,
            turns=turns_completed,
            stop_reason=stop_reason,
        )
        print(
            f"[collab] halted: {turns_completed} turns, reason={stop_reason}, exchange={exchange_path}"
        )
        print(f"[collab] last active target: {last_active_target}")

    def _halt_listener(self, halt_event: threading.Event, stop_event: threading.Event) -> None:
        """Watch stdin for `/halt` while collab mode runs.

        Args:
            halt_event: Set when halt is requested.
            stop_event: Set when listener should stop.
        """
        if not sys.stdin.isatty():
            return

        while not stop_event.is_set() and not halt_event.is_set():
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            if not ready:
                continue
            line = sys.stdin.readline()
            if not line:
                continue
            if line.strip() == "/halt":
                halt_event.set()
                print("\n[collab] halt requested")

    def _write_exchange_log(
        self,
        workspace_root: Path,
        turn_records: list[tuple[PendingSend, ResponseTurn]],
        initial_message: str,
        started_at: datetime,
        turns: int,
        stop_reason: str,
    ) -> Path:
        """Persist collab exchange log.

        Args:
            workspace_root: Workspace root for state output.
            turn_records: Collected turn send/response tuples.
            initial_message: User collab prompt.
            started_at: Collab start timestamp.
            turns: Turns completed.
            stop_reason: Terminal reason string.

        Returns:
            Path to written markdown file.
        """
        timestamp = datetime.now().strftime("%y%m%d-%H%M%S")
        output_dir = exchanges_dir(workspace_root)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{timestamp}.md"

        header_message = initial_message.strip().replace("\n", " ")[:80]
        lines = [
            f"# Collaboration: {header_message}",
            "",
            f"Started: {started_at.isoformat()}",
            "Agents: claude ↔ codex",
            f"Turns: {turns}",
            f"Stop reason: {stop_reason}",
            "",
        ]

        round_number = 1
        for index in range(0, len(turn_records), 2):
            lines.extend(
                [
                    f"## Round {round_number}",
                    "",
                    f"### → {turn_records[index][0].target_agent}",
                    turn_records[index][0].sent_text,
                    "",
                    f"### ← {turn_records[index][1].agent}",
                    turn_records[index][1].text,
                    "",
                ]
            )

            if index + 1 < len(turn_records):
                lines.extend(
                    [
                        f"### → {turn_records[index + 1][0].target_agent}",
                        turn_records[index + 1][0].sent_text,
                        "",
                        f"### ← {turn_records[index + 1][1].agent}",
                        turn_records[index + 1][1].text,
                        "",
                    ]
                )
            round_number += 1

        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output_path

    def _print_status(self, workspace_root: Path, participants: SessionParticipants) -> None:
        """Print concise runtime status."""
        snapshot = cursor_snapshot(workspace_root)
        print("participants:")
        print(f"  claude: pane={participants.claude.tmux_pane}  session={participants.claude.session_id}")
        print(f"    log: {participants.claude.session_file}")
        print(f"  codex:  pane={participants.codex.tmux_pane}  session={participants.codex.session_id}")
        print(f"    log: {participants.codex.session_file}")
        print("cursors:")
        print(f"  read-claude: {snapshot['read-claude']}  read-codex: {snapshot['read-codex']}")
        print(f"  to-claude: {snapshot['to-claude']}  to-codex: {snapshot['to-codex']}")

    @staticmethod
    def _print_help() -> None:
        """Print command usage."""
        print("usage:")
        print("  python3 -m claodex [directory]")
        print("  python3 -m claodex attach [directory]")


def parse_collab_request(command_text: str, default_start: str) -> CollabRequest:
    """Parse `/collab` command arguments.

    Args:
        command_text: Raw command text.
        default_start: Default start agent when option omitted.

    Returns:
        Parsed collaboration request.
    """
    import shlex

    pieces = shlex.split(command_text)
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

        break

    message = " ".join(pieces[index:]).strip()
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

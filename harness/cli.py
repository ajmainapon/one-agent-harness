"""Command-line interface for One Agent Harness (`harness`)."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from typing import List, Optional

from harness import __version__
from harness.engine import AGENT_ALIASES, HarnessEngine, normalize_agent_name
from harness.formatting import Colors, agent_badge, relative_time, shorten_path, style
from harness.installer import install_all
from harness.mcp_server import MCPServer
from harness.platform_utils import configure_console, copy_to_clipboard
from harness.watcher import ContextWatcher


def _copy_to_clipboard(text: str) -> bool:
    return copy_to_clipboard(text)


def cmd_status(args: argparse.Namespace, sessions=None) -> int:
    engine = HarnessEngine()
    watcher = ContextWatcher(engine)
    d_status = watcher.daemon_status()

    print(style("╭──────────────────────────────────────────────────────────────────────────────────────────╮", Colors.CYAN))
    print(
        style("│ ", Colors.CYAN)
        + style("One Agent Harness", Colors.BOLD, Colors.WHITE)
        + " — Universal Context Bridge (Claude Code · Antigravity · Codex · Grok)"
        + style(" │", Colors.CYAN)
    )
    print(style("╰──────────────────────────────────────────────────────────────────────────────────────────╯", Colors.CYAN))

    if d_status["running"]:
        print(
            f"  {style('● Auto-Sync Daemon:', Colors.GREEN, Colors.BOLD)} RUNNING (PID {d_status['pid']})  "
            f"|  {style('Log:', Colors.DIM)} {shorten_path(str(d_status['log_file']))}"
        )
    else:
        print(
            f"  {style('○ Auto-Sync Daemon:', Colors.YELLOW, Colors.BOLD)} STOPPED (run `harness watch --daemon`)"
        )
    print()

    if sessions is None:
        sessions = engine.list_sessions(agent=args.agent, cwd=args.cwd, limit=args.limit)
    if not sessions:
        print("  No matching sessions found. Start a conversation in a supported agent, or check your --agent / --cwd filters.")
        return 0

    print(style(f"  Recent Sessions (use #number or keyword in any command):", Colors.BOLD))
    print("  " + "─" * 92)
    for idx, s in enumerate(sessions, 1):
        cwd_short = shorten_path(s.cwd)
        rel_ts = relative_time(s.mtime)
        idx_str = style(f"#{idx:<2d}", Colors.BOLD, Colors.WHITE)
        badge = agent_badge(s.agent)
        title_str = style(s.title[:56], Colors.BOLD)
        time_str = style(f"{rel_ts:>10s}", Colors.DIM)
        print(f"  {idx_str} {badge} {time_str}  {title_str}")
        print(f"      {style('folder:', Colors.DIM)} {cwd_short}   {style(f'({s.turn_count} turns, id={s.session_id[:8]})', Colors.DIM)}")
    print("  " + "─" * 92)
    print(
        f"  {style('Quick Commands:', Colors.BOLD)} "
        f"`harness pick` (interactive menu) | "
        f"`harness copy 1` | `harness codex 3` | `harness claude 2` | `harness agy 1`"
    )
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    """Interactive terminal session picker."""
    engine = HarnessEngine()
    sessions = engine.list_sessions(limit=12)
    if not sessions:
        print("No sessions found.")
        return 1

    cmd_status(argparse.Namespace(agent=None, cwd=None, limit=12), sessions=sessions)
    print()
    try:
        raw_choice = input(style("Select session # or keyword [default=1, q=quit]: ", Colors.BOLD, Colors.CYAN)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0

    if raw_choice.lower() in ("q", "quit", "exit"):
        return 0
    selector = raw_choice or "1"

    index = selector.lstrip("#")
    if index.isdigit():
        number = int(index)
        resolved = sessions[number - 1] if 1 <= number <= len(sessions) else None
    else:
        resolved = engine.resolve_session(selector=selector)
    if not resolved:
        print(f"Could not find session matching '{selector}'.")
        return 1

    print(
        f"\nSelected: {agent_badge(resolved.agent)} {style(resolved.title, Colors.BOLD)} "
        f"({shorten_path(resolved.cwd)})"
    )
    print(
        "  [c] Switch & resume in Claude Code\n"
        "  [x] Switch & resume in OpenAI Codex\n"
        "  [a] Open & sync to Antigravity\n"
        "  [g] Switch & resume in Grok Build CLI\n"
        "  [p] Copy full context to Clipboard (Cmd+V)\n"
        "  [v] View full handoff summary in terminal"
    )
    try:
        act = input(style("Choose action [c/x/a/g/p/v, default=p]: ", Colors.BOLD, Colors.CYAN)).strip().lower() or "p"
    except (EOFError, KeyboardInterrupt):
        print()
        return 0

    action_map = {"c": "claude", "x": "codex", "a": "antigravity", "g": "grok"}
    if act in action_map:
        return cmd_switch(
            argparse.Namespace(
                to_agent=action_map[act],
                from_agent=resolved.agent,
                session=resolved.session_id,
                query=None,
                cwd=None,
                max_turns=25,
                no_launch=getattr(args, "no_launch", False),
                dry_run=False,
                copy=True,
            )
        )
    if act == "v":
        return cmd_handoff(
            argparse.Namespace(
                source_agent=resolved.agent,
                session=resolved.session_id,
                query=None,
                cwd=None,
                max_turns=15,
                stdout=True,
                copy=True,
                update_agent_md=False,
            )
        )
    return cmd_handoff(
        argparse.Namespace(
            source_agent=resolved.agent,
            session=resolved.session_id,
            query=None,
            cwd=None,
            max_turns=25,
            stdout=False,
            copy=True,
            update_agent_md=True,
        )
    )


def cmd_handoff(args: argparse.Namespace) -> int:
    engine = HarnessEngine()
    query_val = " ".join(args.query).strip() if isinstance(getattr(args, "query", None), list) else getattr(args, "query", None)
    bundle = engine.extract_bundle(
        source_agent=args.source_agent,
        session_id=args.session,
        cwd=args.cwd,
        max_turns=args.max_turns,
        query=query_val or None,
    )
    if not bundle:
        print("No matching session found. Run `harness status` with the same filters, then use a session ID or number.", file=sys.stderr)
        return 1

    read_only = getattr(args, "read_only", False)
    live_path = None
    if not read_only:
        try:
            live_path = engine.sync_workspace_context(
                bundle, target_cwd=args.cwd or bundle.cwd,
                update_agent_md=args.update_agent_md,
            )
            for warning in engine.sync_warnings:
                print(f"Warning: {warning}", file=sys.stderr)
        except OSError as exc:
            print(f"Warning: Context loaded, but could not be saved: {exc}.", file=sys.stderr)
    md_text = bundle.to_markdown(include_full_artifacts=True, max_turns=args.max_turns)

    copied = False
    if args.copy and not read_only:
        copied = _copy_to_clipboard(md_text)
        if not copied:
            print("Clipboard unavailable. Use `harness handoff --read-only` to print context, or open the saved context file.", file=sys.stderr)

    if args.stdout or read_only or (not live_path and not copied):
        print(md_text)
    else:
        print(
            f"{style('✔ Extracted Context:', Colors.GREEN, Colors.BOLD)} "
            f"{agent_badge(bundle.source_agent)} {style(bundle.title, Colors.BOLD)}"
        )
        print(f"  • Session ID       : `{bundle.session_id}`")
        print(f"  • Working Folder   : `{shorten_path(bundle.cwd)}`")
        print(f"  • Turns Preserved  : {len(bundle.turns)} (of {bundle.total_turn_count or len(bundle.turns)})")
        print(f"  • Plans/Artifacts  : {len(bundle.artifacts_and_plans)}")
        print(f"  • Files Modified   : {len(bundle.files_modified)}")
        if live_path:
            print(f"  • Live Sync File   : `{shorten_path(live_path)}`")
        if copied:
            print(f"  • Clipboard        : {style('Copied context to your system clipboard', Colors.CYAN)}")
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    engine = HarnessEngine()
    query_val = " ".join(args.query).strip() if isinstance(getattr(args, "query", None), list) else getattr(args, "query", None)
    try:
        res = engine.switch_session(
            to_agent=args.to_agent,
            from_agent=args.from_agent,
            session_id=args.session,
            cwd=args.cwd,
            max_turns=args.max_turns,
            dry_run=args.dry_run,
            query=query_val or None,
        )
    except Exception as exc:
        print(f"Error switching session: {exc}", file=sys.stderr)
        return 1

    bundle = res["bundle"]
    launch_cmd: List[str] = res["launch_cmd"]
    cmd_str = " ".join(launch_cmd)

    for warning in res["warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)
    heading = "Preview — no files or clipboard changed" if args.dry_run else "✔ Cross-Agent Session Switch Ready"
    print(style(heading, Colors.GREEN, Colors.BOLD))
    print(f"  • From             : {agent_badge(res['source_agent'])} {style(bundle.title, Colors.BOLD)}")
    print(f"  • To               : {agent_badge(res['target_agent'])}")
    print(f"  • Workspace Folder : `{shorten_path(res['cwd'])}`")
    if res["live_context_path"]:
        print(f"  • Live Context File: `{shorten_path(res['live_context_path'])}`")
    if not args.dry_run:
        print(f"  • Resume Command   : `{style(cmd_str, Colors.BOLD, Colors.CYAN)}`")

    if args.copy and not args.dry_run:
        if _copy_to_clipboard(bundle.to_markdown(include_full_artifacts=True, max_turns=args.max_turns)):
            print("  • Clipboard        : Copied context to your system clipboard")
        else:
            print("Clipboard unavailable. Your context is saved in the file above.", file=sys.stderr)

    if args.dry_run or args.no_launch:
        return 0

    bin_name = launch_cmd[0] if launch_cmd else ""
    bin_path = shutil.which(bin_name)
    if not bin_path:
        print(
            f"\nNote: `{bin_name}` binary not found in PATH; context has been injected natively and saved to {res['live_context_path']}."
        )
        return 0

    print(f"\nLaunching `{cmd_str}` in {res['cwd']}...\n")
    if not os.path.isdir(res["cwd"]):
        print(f"Cannot launch: workspace {res['cwd']!r} no longer exists. Context is saved; use --cwd with an existing workspace.", file=sys.stderr)
        return 1
    os.chdir(res["cwd"])
    os.execvp(bin_path, launch_cmd)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    watcher = ContextWatcher()
    if args.stop:
        stopped = watcher.stop_daemon()
        print("Stopped background watcher daemon." if stopped else "Background watcher daemon was not running.")
        return 0

    if args.status:
        st = watcher.daemon_status()
        if st["running"]:
            print(f"Watcher daemon is RUNNING (PID {st['pid']}). Log: {st['log_file']}")
        else:
            print("Watcher daemon is STOPPED.")
        return 0

    if args.daemon:
        pid = watcher.start_daemon(interval=args.interval, cwd=args.cwd)
        print(f"Started One Agent Harness Auto-Sync Watcher daemon (PID {pid}).")
        print(f"Log file: {watcher.log_file}")
        return 0

    print(f"Starting foreground Auto-Sync Watcher (polling every {args.interval}s)... Press Ctrl+C to stop.")
    try:
        watcher.run_loop(interval=args.interval, cwd=args.cwd)
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    engine = HarnessEngine()
    q_str = " ".join(args.query) if isinstance(args.query, list) else str(args.query)
    matches = engine.search_history(query=q_str, agent=args.agent, limit=args.limit)
    if not matches:
        print(f"No matches found for '{q_str}'.")
        return 0

    print(f"Found {len(matches)} matches across agents for '{q_str}':")
    print("─" * 92)
    for idx, m in enumerate(matches, 1):
        print(f"{idx:2d}. {agent_badge(m['agent'])} {m['updated_at']} | id={m['session_id'][:12]} | {style(m['title'], Colors.BOLD)}")
        print(f"    cwd  : {shorten_path(m['cwd'])}")
        print(f"    match: {m['snippet']}")
    print("─" * 92)
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    engine = HarnessEngine()
    entry = engine.save_checkpoint(summary=args.summary, next_steps=args.next_steps or "", cwd=args.cwd)
    print(f"Saved checkpoint at {entry['timestamp']} for `{entry['cwd']}`.")
    return 0


def cmd_install(_args: argparse.Namespace) -> int:
    res = install_all()
    print("=== One Agent Harness Installed Successfully ===")
    print("\n1. CLI Executables:")
    for p in res["cli_binaries"]:
        print(f"   - {p}")
    print("\n2. MCP Server Registered In:")
    for p in res["mcp_configs"]:
        print(f"   - {p}")
    print("\n3. Universal `/handoff` & `$handoff` Skill Installed In:")
    for p in res["skills"]:
        print(f"   - {p}")
    if res["backups"]:
        print("\n4. Config Backups Created:")
        for p in res["backups"]:
            print(f"   - {p}")
    return 0


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("enter a positive whole number")
    if number < 1:
        raise argparse.ArgumentTypeError("enter a positive whole number")
    return number


def _positive_interval(value: str) -> float:
    import math
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("enter a positive number of seconds")
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("enter a positive number of seconds")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="One Agent Harness: Universal Cross-Agent Context Bridge for Claude Code, Antigravity, Codex & Grok Build CLI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    # status
    p_status = sub.add_parser("status", aliases=["ls", "list"], help="List recent sessions across all 4 agents")
    p_status.add_argument("--agent", "-a", help="Filter by agent ('claude', 'antigravity', 'codex', 'grok')")
    p_status.add_argument("--cwd", help="Filter by workspace path")
    p_status.add_argument("--limit", "-n", type=_positive_int, default=12, help="Max sessions to display")
    p_status.set_defaults(func=cmd_status)

    # pick / menu
    p_pick = sub.add_parser("pick", aliases=["menu", "i"], help="Interactive terminal menu to pick & switch sessions")
    p_pick.add_argument("--no-launch", action="store_true", help="Do not launch target CLI after injecting")
    p_pick.set_defaults(func=cmd_pick)

    # copy
    p_copy = sub.add_parser("copy", aliases=["cp"], help="Copy a session's full context bundle to clipboard (by #number or keyword)")
    p_copy.add_argument("query", nargs="*", help="Optional session #number ('1', '2'), agent ('claude'), or project keyword ('auth refactor')")
    p_copy.add_argument("--from", dest="source_agent", help="Source agent")
    p_copy.add_argument("--session", help="Specific session ID prefix")
    p_copy.add_argument("--cwd", help="Workspace directory filter")
    p_copy.add_argument("--max-turns", type=_positive_int, default=25, help="Max verbatim turns")
    p_copy.set_defaults(
        func=lambda a: cmd_handoff(
            argparse.Namespace(
                source_agent=a.source_agent,
                session=a.session,
                query=a.query,
                cwd=a.cwd,
                max_turns=a.max_turns,
                stdout=False,
                copy=True,
                update_agent_md=True,
            )
        )
    )

    # handoff
    p_handoff = sub.add_parser("handoff", help="Extract complete context bundle and sync .harness/LIVE_CONTEXT.md")
    p_handoff.add_argument("query", nargs="*", help="Optional session #number ('1', '2'), agent ('claude'), or project keyword")
    p_handoff.add_argument("--from", dest="source_agent", help="Source agent")
    p_handoff.add_argument("--session", help="Specific session ID prefix")
    p_handoff.add_argument("--cwd", help="Workspace directory filter/target")
    p_handoff.add_argument("--max-turns", type=_positive_int, default=25, help="Max verbatim turns to include")
    p_handoff.add_argument("--stdout", action="store_true", help="Print the full Markdown bundle to stdout")
    p_handoff.add_argument("--copy", action="store_true", default=True, help="Copy Markdown bundle to clipboard (default: True)")
    p_handoff.add_argument("--no-copy", dest="copy", action="store_false", help="Do not copy to clipboard")
    p_handoff.add_argument("--update-agent-md", action="store_true", default=True, help="Update AGENTS.md/CLAUDE.md pointer if present")
    p_handoff.add_argument("--no-update-agent-md", dest="update_agent_md", action="store_false", help="Leave AGENTS.md and CLAUDE.md unchanged")
    p_handoff.add_argument("--read-only", action="store_true", help="Print context without saving files or changing the clipboard")
    p_handoff.set_defaults(func=cmd_handoff)

    # switch / to
    p_switch = sub.add_parser("switch", aliases=["to"], help="Convert session into target agent's native format and launch it")
    p_switch.add_argument("query", nargs="*", help="Optional session #number ('1', '2') or project keyword")
    p_switch.add_argument("--to", dest="to_agent", required=False, help="Target agent ('claude', 'antigravity', 'codex', 'grok')")
    p_switch.add_argument("--from", dest="from_agent", help="Source agent (auto-detected if omitted)")
    p_switch.add_argument("--session", help="Specific source session ID prefix")
    p_switch.add_argument("--cwd", help="Workspace directory")
    p_switch.add_argument("--max-turns", type=_positive_int, default=25, help="Max verbatim turns to include")
    p_switch.add_argument("--no-launch", action="store_true", help="Inject session natively without launching the CLI")
    p_switch.add_argument("--dry-run", action="store_true", help="Preview conversion without changing files or clipboard")
    p_switch.add_argument("--copy", action="store_true", default=True, help="Also copy handoff markdown to clipboard")
    p_switch.add_argument("--no-copy", dest="copy", action="store_false", help="Do not copy to clipboard")
    p_switch.set_defaults(func=cmd_switch)

    # Direct agent shorthands: `harness codex [query]`, `harness claude [query]`, `harness agy [query]`, `harness grok [query]`
    for shorthand, canonical_target in [
        ("codex", "codex"),
        ("claude", "claude"),
        ("antigravity", "antigravity"),
        ("agy", "antigravity"),
        ("grok", "grok"),
    ]:
        p_sh = sub.add_parser(shorthand, help=f"Shorthand: switch session directly to {canonical_target.capitalize()}")
        p_sh.add_argument("query", nargs="*", help="Optional session #number ('1', '2') or project keyword ('auth refactor')")
        p_sh.add_argument("--from", dest="from_agent", help="Optional source agent")
        p_sh.add_argument("--session", help="Optional session ID")
        p_sh.add_argument("--cwd", help="Optional workspace directory")
        p_sh.add_argument("--max-turns", type=_positive_int, default=25, help="Max verbatim turns")
        p_sh.add_argument("--no-launch", action="store_true", help="Inject session natively without launching CLI")
        p_sh.add_argument("--dry-run", action="store_true", help="Preview conversion without changing files or clipboard")
        p_sh.add_argument("--copy", action="store_true", default=True, help="Copy context to clipboard")
        p_sh.add_argument("--no-copy", dest="copy", action="store_false", help="Do not copy to clipboard")
        p_sh.set_defaults(func=cmd_switch, to_agent=canonical_target)

    # watch
    p_watch = sub.add_parser("watch", help="Run the real-time Auto-Sync Watcher daemon")
    p_watch.add_argument("--daemon", action="store_true", help="Run watcher in the background")
    p_watch.add_argument("--stop", action="store_true", help="Stop background watcher daemon")
    p_watch.add_argument("--status", action="store_true", help="Check background watcher daemon status")
    p_watch.add_argument("--interval", type=_positive_interval, default=30.0, help="Polling interval in seconds (default: 30)")
    p_watch.add_argument("--cwd", help="Optional specific workspace directory to watch")
    p_watch.set_defaults(func=cmd_watch)

    # search
    p_search = sub.add_parser("search", aliases=["find"], help="Search across all 4 agents' conversation history and modified files")
    p_search.add_argument("query", nargs="+", help="Keyword, filename, or phrase to search")
    p_search.add_argument("--agent", "-a", help="Optional agent filter")
    p_search.add_argument("--limit", "-n", type=_positive_int, default=15, help="Max results")
    p_search.set_defaults(func=cmd_search)

    # checkpoint
    p_cp = sub.add_parser("checkpoint", aliases=["note"], help="Save an explicit checkpoint note for the next agent")
    p_cp.add_argument("summary", help="Summary of current state and decisions")
    p_cp.add_argument("--next", dest="next_steps", default="", help="Next steps for the receiving agent")
    p_cp.add_argument("--cwd", help="Workspace directory")
    p_cp.set_defaults(func=cmd_checkpoint)

    # install
    p_install = sub.add_parser("install", help="Install CLI binary, MCP server, and /handoff skill across all 4 agents")
    p_install.set_defaults(func=cmd_install)

    # mcp
    p_mcp = sub.add_parser("mcp", help="Run the STDIO Model Context Protocol server")
    p_mcp.set_defaults(func=lambda _a: MCPServer().serve_stdio() or 0)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        return cmd_status(argparse.Namespace(agent=None, cwd=None, limit=10))

    # Handle `harness to codex 3` where 'codex' is passed as the first word of args.query
    if getattr(args, "command", None) in ("switch", "to") and not getattr(args, "to_agent", None):
        q_list = list(getattr(args, "query", []) or [])
        if q_list and q_list[0].lower() in AGENT_ALIASES:
            args.to_agent = normalize_agent_name(q_list.pop(0))
            args.query = q_list
        elif len(q_list) >= 2 and q_list[-1].lower() in AGENT_ALIASES:
            args.to_agent = normalize_agent_name(q_list.pop(-1))
            args.query = q_list
        else:
            print("Error: specify target agent (e.g. `harness codex 3` or `harness switch --to codex`).", file=sys.stderr)
            return 1

    try:
        return int(args.func(args) or 0)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

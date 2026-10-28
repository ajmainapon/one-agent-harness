"""Real-time Auto-Sync Watcher Daemon for One Agent Harness."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

from harness.adapters import normalize_path

from harness.engine import HarnessEngine
from harness.platform_utils import is_pid_running, spawn_detached_process, terminate_pid


class ContextWatcher:
    """Monitors all 4 agent platforms and auto-syncs .harness/LIVE_CONTEXT.md on changes."""

    def __init__(self, engine: Optional[HarnessEngine] = None) -> None:
        self.engine = engine or HarnessEngine()
        self.last_seen: Dict[str, Tuple[str, str, float]] = {}  # cwd -> (agent, session_id, mtime)
        self.fallback_seen: Dict[str, Tuple[Tuple[str, str, float], bool]] = {}
        self.pid_file = os.path.join(self.engine.harness_home, "watcher.pid")
        self.log_file = os.path.join(self.engine.harness_home, "watcher.log")

    def sync_once(
        self,
        cwd: Optional[str] = None,
        update_agent_md: bool = True,
        force: bool = False,
    ) -> Optional[str]:
        """Sync all changed workspaces and return the last saved path, if any."""
        paths = self.sync_changed_workspaces(cwd, update_agent_md, force)
        return paths[-1] if paths else None

    def sync_changed_workspaces(
        self,
        cwd: Optional[str] = None,
        update_agent_md: bool = True,
        force: bool = False,
    ) -> List[str]:
        """Sync the latest session in every workspace, without starving older ones."""
        sessions = self.engine.list_sessions(cwd=cwd, limit=None)
        newest_by_workspace = {}
        for session in sessions:
            target_cwd = normalize_path(session.cwd or cwd or self.engine.harness_home)
            newest_by_workspace.setdefault(target_cwd, session)

        paths: List[str] = []
        # Save oldest first so the global copy represents the newest activity.
        for target_cwd, latest in reversed(list(newest_by_workspace.items())):
            signature = (latest.agent, latest.session_id, latest.mtime)
            dir_exists = os.path.isdir(target_cwd)
            if not force:
                if self.last_seen.get(target_cwd) == signature:
                    continue
                if self.fallback_seen.get(target_cwd) == (signature, dir_exists):
                    continue
            try:
                bundle = self.engine.extract_session_bundle(
                    latest, max_turns=25, include_other_sessions=False,
                )
                if not bundle:
                    continue
                live_path = self.engine.sync_workspace_context(
                    bundle, target_cwd=target_cwd, update_agent_md=update_agent_md,
                    update_global=(latest is sessions[0]),
                )
                for warning in self.engine.sync_warnings:
                    print(f"[watcher] {warning}", file=sys.stderr, flush=True)
                # Track primary saves in last_seen and fallback saves in fallback_seen
                # so unavailable workspaces are only retried if the folder reappears or session updates.
                expected = os.path.join(target_cwd, ".harness", "LIVE_CONTEXT.md")
                if live_path == expected or target_cwd == self.engine.harness_home:
                    self.last_seen[target_cwd] = signature
                    self.fallback_seen.pop(target_cwd, None)
                else:
                    self.fallback_seen[target_cwd] = (signature, dir_exists)
                paths.append(live_path)
            except OSError as exc:
                print(f"[watcher] Could not sync {target_cwd!r}: {exc}. Will retry.", file=sys.stderr, flush=True)
        return paths

    def run_loop(
        self,
        interval: float = 30.0,
        cwd: Optional[str] = None,
        update_agent_md: bool = True,
        max_iterations: Optional[int] = None,
    ) -> None:
        """Continuously watch agent session folders and sync changes."""
        iteration = 0
        while True:
            try:
                synced_paths = self.sync_changed_workspaces(
                    cwd=cwd,
                    update_agent_md=update_agent_md,
                    force=(iteration == 0),
                )
                for synced_path in synced_paths:
                    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                    print(f"[{now_str}] Synced latest cross-agent context -> {synced_path}", flush=True)
            except Exception as exc:
                print(f"[watcher] error: {exc}", file=sys.stderr, flush=True)

            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break
            time.sleep(interval)

    def start_daemon(self, interval: float = 30.0, cwd: Optional[str] = None) -> int:
        """Spawn the watcher in the background and record its PID across Windows, macOS, and Linux."""
        status = self.daemon_status()
        if status["running"]:
            return int(status["pid"])  # type: ignore[arg-type]

        cmd = [
            sys.executable,
            "-m",
            "harness.cli",
            "watch",
            "--interval",
            str(interval),
        ]
        if cwd:
            cmd.extend(["--cwd", cwd])

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        installed_lib = os.path.join(self.engine.harness_home, "lib")
        env = os.environ.copy()
        env["PYTHONPATH"] = (
            project_root + os.pathsep + installed_lib + os.pathsep + env.get("PYTHONPATH", "")
        )

        os.makedirs(self.engine.harness_home, exist_ok=True)
        pid = spawn_detached_process(
            cmd=cmd,
            cwd=self.engine.harness_home,
            env=env,
            log_file=self.log_file,
        )
        with open(self.pid_file, "w", encoding="utf-8") as fp:
            fp.write(str(pid))
        return pid

    def stop_daemon(self) -> bool:
        """Stop a running background watcher daemon if active."""
        status = self.daemon_status()
        if not status["running"]:
            if os.path.isfile(self.pid_file):
                os.remove(self.pid_file)
            return False
        pid = int(status["pid"])  # type: ignore[arg-type]
        terminate_pid(pid)
        if os.path.isfile(self.pid_file):
            os.remove(self.pid_file)
        return True

    def daemon_status(self) -> Dict[str, object]:
        """Check whether the background watcher daemon is running across Windows, macOS, and Linux."""
        if not os.path.isfile(self.pid_file):
            return {"running": False, "pid": None, "log_file": self.log_file}
        try:
            with open(self.pid_file, "r", encoding="utf-8") as fp:
                pid = int(fp.read().strip())
            if is_pid_running(pid):
                return {"running": True, "pid": pid, "log_file": self.log_file}
            return {"running": False, "pid": None, "log_file": self.log_file}
        except (ValueError, OSError):
            return {"running": False, "pid": None, "log_file": self.log_file}

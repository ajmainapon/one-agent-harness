"""Real-time Auto-Sync Watcher Daemon for One Agent Harness."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import sys
import time
from typing import Dict, Optional, Tuple

from harness.engine import HarnessEngine
from harness.platform_utils import is_pid_running, spawn_detached_process, terminate_pid


class ContextWatcher:
    """Monitors all 4 agent platforms and auto-syncs .harness/LIVE_CONTEXT.md on changes."""

    def __init__(self, engine: Optional[HarnessEngine] = None) -> None:
        self.engine = engine or HarnessEngine()
        self.last_seen: Dict[str, Tuple[str, float]] = {}  # cwd -> (session_id, mtime)
        self.pid_file = os.path.join(self.engine.harness_home, "watcher.pid")
        self.log_file = os.path.join(self.engine.harness_home, "watcher.log")

    def sync_once(
        self,
        cwd: Optional[str] = None,
        update_agent_md: bool = True,
        force: bool = False,
    ) -> Optional[str]:
        """Check for the newest session activity and sync .harness/LIVE_CONTEXT.md if changed."""
        sessions = self.engine.list_sessions(cwd=cwd, limit=5)
        if not sessions:
            return None

        latest = sessions[0]
        target_cwd = cwd or latest.cwd or os.getcwd()
        prev = self.last_seen.get(target_cwd)

        if not force and prev and prev[0] == latest.session_id and prev[1] >= latest.mtime:
            return None

        bundle = self.engine.extract_bundle(
            source_agent=latest.agent,
            session_id=latest.session_id,
            cwd=target_cwd,
            max_turns=25,
        )
        if not bundle:
            return None

        live_path = self.engine.sync_workspace_context(
            bundle,
            target_cwd=target_cwd,
            update_agent_md=update_agent_md,
        )
        self.last_seen[target_cwd] = (latest.session_id, latest.mtime)
        return live_path

    def run_loop(
        self,
        interval: float = 2.0,
        cwd: Optional[str] = None,
        update_agent_md: bool = True,
        max_iterations: Optional[int] = None,
    ) -> None:
        """Continuously watch agent session folders and sync changes."""
        iteration = 0
        while True:
            try:
                synced_path = self.sync_once(
                    cwd=cwd,
                    update_agent_md=update_agent_md,
                    force=(iteration == 0),
                )
                if synced_path:
                    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                    print(f"[{now_str}] Synced latest cross-agent context -> {synced_path}", flush=True)
            except Exception as exc:
                print(f"[watcher] error: {exc}", file=sys.stderr, flush=True)

            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break
            time.sleep(interval)

    def start_daemon(self, interval: float = 2.0, cwd: Optional[str] = None) -> int:
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

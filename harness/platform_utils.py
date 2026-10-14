"""Cross-platform OS utilities for Linux, macOS, and Windows."""

from __future__ import annotations

import base64
import ctypes
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
from typing import Dict, List, Optional


IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def is_wsl() -> bool:
    """Return True if running inside Windows Subsystem for Linux (WSL)."""
    if not IS_LINUX:
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as fp:
            return "microsoft" in fp.read().lower()
    except OSError:
        return False


def configure_console() -> None:
    """Ensure UTF-8 output and ANSI color support on Windows, macOS, and Linux."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass

    if IS_WINDOWS:
        # Enable Windows 10/11 Virtual Terminal Processing for ANSI escape sequences
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            for std_handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
                handle = kernel32.GetStdHandle(std_handle_id)
                if handle and handle != -1:
                    mode = ctypes.c_ulong()
                    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard across macOS, Windows, Linux (X11/Wayland/WSL), and SSH (OSC 52)."""
    if not text:
        return False
    data = text.encode("utf-8")

    # 1. macOS: pbcopy
    if IS_MACOS and shutil.which("pbcopy"):
        try:
            subprocess.run(["pbcopy"], input=data, check=True, timeout=3)
            return True
        except Exception:
            pass

    # 2. Windows native: PowerShell Set-Clipboard (UTF-8 safe) or clip.exe
    if IS_WINDOWS:
        if shutil.which("powershell"):
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", "$input | Set-Clipboard"],
                    input=data,
                    check=True,
                    timeout=4,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                )
                return True
            except Exception:
                pass
        if shutil.which("clip") or shutil.which("clip.exe"):
            try:
                subprocess.run(
                    ["clip.exe"],
                    input=data,
                    check=True,
                    timeout=3,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                )
                return True
            except Exception:
                pass

    # 3. Linux / WSL / BSD clipboard tools
    clipboard_cmds: List[List[str]] = []
    if is_wsl() and shutil.which("clip.exe"):
        clipboard_cmds.append(["clip.exe"])
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        clipboard_cmds.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            clipboard_cmds.append(["xclip", "-selection", "clipboard"])
        if shutil.which("xsel"):
            clipboard_cmds.append(["xsel", "--clipboard", "--input"])
    # Also try wl-copy / xclip / xsel / pbcopy if available even if env vars weren't checked yet
    for fallback_cmd in (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["pbcopy"],
    ):
        if shutil.which(fallback_cmd[0]) and fallback_cmd not in clipboard_cmds:
            clipboard_cmds.append(fallback_cmd)

    for cmd in clipboard_cmds:
        try:
            subprocess.run(cmd, input=data, check=True, timeout=3, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue

    # 4. OSC 52 terminal escape sequence fallback (works in SSH/headless sessions on modern terminals)
    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty() and len(data) <= 100_000:
        try:
            b64 = base64.b64encode(data).decode("ascii")
            sys.stdout.write(f"\033]52;c;{b64}\a")
            sys.stdout.flush()
            return True
        except Exception:
            pass

    return False


def is_pid_running(pid: int) -> bool:
    """Check whether a process with the given PID is currently running on Windows, macOS, or Linux."""
    if pid <= 0:
        return False

    if IS_WINDOWS:
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                # STILL_ACTIVE = 259
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return int(exit_code.value) == 259
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def terminate_pid(pid: int) -> bool:
    """Terminate a background process by PID across Windows, macOS, and Linux."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            return True
        except Exception:
            pass
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def spawn_detached_process(cmd: List[str], cwd: str, env: Dict[str, str], log_file: str) -> int:
    """Spawn a background daemon process detached from the current terminal across Windows, macOS, and Linux."""
    log_fp = open(log_file, "a", encoding="utf-8")
    if IS_WINDOWS:
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=log_fp,
            creationflags=creationflags,
            close_fds=True,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=log_fp,
            start_new_session=True,
            close_fds=True,
        )
    return int(proc.pid)


def sqlite_ro_connect(db_path: str, timeout: float = 2.0) -> sqlite3.Connection:
    """Open a read-only SQLite connection safely on Windows, macOS, and Linux."""
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=timeout)
    except Exception:
        return sqlite3.connect(db_path, timeout=timeout)


def get_default_agent_homes() -> Dict[str, str]:
    """Return default or environment-overridden home directories for all supported agents."""
    return {
        "claude": os.path.expanduser(
            os.environ.get("HARNESS_CLAUDE_HOME")
            or os.environ.get("CLAUDE_CONFIG_DIR")
            or "~/.claude"
        ),
        "antigravity": os.path.expanduser(
            os.environ.get("HARNESS_AGY_HOME")
            or os.environ.get("ANTIGRAVITY_HOME")
            or "~/.gemini/antigravity"
        ),
        "codex": os.path.expanduser(
            os.environ.get("HARNESS_CODEX_HOME")
            or os.environ.get("CODEX_HOME")
            or "~/.codex"
        ),
        "grok": os.path.expanduser(
            os.environ.get("HARNESS_GROK_HOME")
            or os.environ.get("GROK_HOME")
            or "~/.grok"
        ),
        "harness": os.path.expanduser(
            os.environ.get("HARNESS_HOME") or "~/.harness"
        ),
    }

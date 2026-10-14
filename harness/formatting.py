"""Human-friendly terminal formatting, ANSI colors, and relative timestamps."""

from __future__ import annotations

import os
import sys
import time
from typing import Optional


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"
    BLUE = "\033[34m"
    RED = "\033[31m"
    WHITE = "\033[97m"


def style(text: str, *codes: str) -> str:
    if not _supports_color() or not codes:
        return text
    return "".join(codes) + text + Colors.RESET


def agent_badge(agent: str) -> str:
    ag = agent.lower()
    label = f" {ag.upper():11s} "
    if not _supports_color():
        return f"[{ag.upper():11s}]"
    if ag == "claude":
        return f"\033[1;35m[{ag.upper():11s}]\033[0m"
    if ag == "antigravity":
        return f"\033[1;36m[{ag.upper():11s}]\033[0m"
    if ag == "codex":
        return f"\033[1;32m[{ag.upper():11s}]\033[0m"
    if ag == "grok":
        return f"\033[1;33m[{ag.upper():11s}]\033[0m"
    return f"[{label}]"


def relative_time(mtime: Optional[ float | str ], now: Optional[float] = None) -> str:
    """Convert a POSIX timestamp or ISO string into a human-friendly relative time string."""
    if not mtime:
        return "unknown"
    if isinstance(mtime, str):
        try:
            mtime = float(mtime)
        except ValueError:
            from harness.adapters import parse_iso_ts

            parsed = parse_iso_ts(mtime)
            if not parsed or parsed <= 0:
                return "unknown"
            mtime = parsed
    if not isinstance(mtime, (int, float)) or mtime <= 0:
        return "unknown"
    ref = now if now is not None else time.time()
    diff = max(0.0, ref - float(mtime))
    if diff < 45:
        return "just now"
    if diff < 3600:
        mins = int(diff // 60)
        return f"{mins}m ago"
    if diff < 86400:
        hours = int(diff // 3600)
        mins = int((diff % 3600) // 60)
        return f"{hours}h {mins}m ago" if hours < 6 and mins > 0 else f"{hours}h ago"
    days = int(diff // 86400)
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days}d ago"
    months = int(days // 30)
    return f"{months}mo ago"


def shorten_path(path: Optional[str]) -> str:
    if not path:
        return "N/A"
    home = os.path.expanduser("~")
    if path.startswith(home):
        return "~" + path[len(home) :]
    return path

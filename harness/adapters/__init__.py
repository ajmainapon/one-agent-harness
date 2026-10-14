"""Platform adapters for reading and injecting sessions across AI agents."""

from __future__ import annotations

from datetime import datetime
import os
import re
from typing import List, Optional
from urllib.parse import unquote, urlparse

from harness.models import ContextBundle, SessionSummary, Turn


def parse_iso_ts(ts_str: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 timestamp string into a POSIX float timestamp."""
    if not ts_str:
        return None
    try:
        clean = ts_str.strip()
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        return datetime.fromisoformat(clean).timestamp()
    except Exception:
        return None


def normalize_path(path: Optional[str]) -> str:
    """Normalize a filesystem path across Linux, macOS, and Windows (decoding file:// URIs and preserving trailing spaces)."""
    if not path:
        return ""
    raw = path.rstrip("\r\n")
    if raw.startswith("file://"):
        parsed = urlparse(raw.replace("\\", "/"))
        raw_path = unquote(parsed.path)
        if re.match(r"^[a-zA-Z]:$", parsed.netloc):
            raw = f"{parsed.netloc}{raw_path}"
        elif re.match(r"^/[a-zA-Z]:[/\\]", raw_path):
            raw = raw_path[1:]
        elif parsed.netloc and parsed.netloc not in ("", "localhost"):
            # UNC path file://server/share/...
            raw = f"//{parsed.netloc}{raw_path}"
        else:
            raw = raw_path

    expanded = os.path.expanduser(raw)
    if os.path.exists(expanded):
        trailing_space = " " if (expanded.endswith(" ") and not os.path.normpath(expanded).endswith(" ")) else ""
        normed = os.path.normpath(expanded) + trailing_space
        if os.path.exists(normed):
            return normed
        return expanded

    stripped = os.path.expanduser(raw.strip())
    if os.path.exists(stripped):
        return os.path.normpath(stripped)
    if os.path.exists(stripped + " "):
        return os.path.normpath(stripped) + " "
    # If it looks like a Windows drive path on a non-Windows host (e.g. during cross-platform tests), normalize slashes
    if re.match(r"^[a-zA-Z]:[/\\]", stripped) and os.sep != "\\":
        return stripped.replace("\\", "/").rstrip("/") or stripped[:3]
    return os.path.normpath(stripped)


def _canon_for_compare(p: str) -> str:
    """Canonicalize a path string for cross-platform comparison."""
    s = p.rstrip(" ").replace("\\", "/")
    if len(s) > 1 and not re.match(r"^[a-zA-Z]:/$", s):
        s = s.rstrip("/")
    # Windows drive paths or Windows OS are case-insensitive
    if os.name == "nt" or re.match(r"^[a-zA-Z]:/", s):
        s = s.lower()
    return s


def paths_match(cwd_filter: Optional[str], session_cwd: Optional[str]) -> bool:
    """Return True if session_cwd matches or is inside/parent of cwd_filter across Linux, macOS, and Windows."""
    if not cwd_filter:
        return True
    if not session_cwd:
        return False

    norm_filter = normalize_path(cwd_filter)
    norm_session = normalize_path(session_cwd)
    if not norm_filter or not norm_session:
        return False

    candidates = [(norm_filter, norm_session)]
    try:
        candidates.append((os.path.realpath(norm_filter), os.path.realpath(norm_session)))
    except OSError:
        pass

    for f_raw, s_raw in candidates:
        f_p = _canon_for_compare(f_raw)
        s_p = _canon_for_compare(s_raw)
        if (
            f_p == s_p
            or s_p.startswith(f_p + "/")
            or f_p.startswith(s_p + "/")
        ):
            return True
    return False


def strip_handoff_prefix(title: str) -> str:
    """Strip any existing [Handoff:...] prefix from a session title to prevent nested prefixes."""
    return re.sub(r"^(?:\[Handoff:[^\]]+\]\s*)+", "", (title or "").strip(), flags=re.IGNORECASE).strip()


def is_meta_handoff_text(text: str) -> bool:
    """Return True if a prompt is purely a /handoff or $handoff invocation, synthetic seed header, or side question."""
    t = text.strip().lower()
    if not t:
        return True
    if t in ("/handoff", "$handoff", "handoff", "summarize", "continue"):
        return True
    if t.startswith("[handoff from "):
        return True
    if "[$handoff]" in t or "skills/handoff/skill.md" in t:
        return True
    if t.startswith("this is a side question from the user."):
        return True
    if re.match(r"^[/\$]?handoff\b", t) and len(t) < 80:
        return True
    return False


def consolidate_turns(raw_turns: List[Turn]) -> List[Turn]:
    """Merge consecutive turns of the same role and record final_text on assistant turns."""
    consolidated: List[Turn] = []
    for t in raw_turns:
        txt = t.content.strip()
        if not consolidated:
            consolidated.append(
                Turn(
                    role=t.role,
                    content=txt,
                    timestamp=t.timestamp,
                    thinking=t.thinking,
                    tool_calls=list(t.tool_calls),
                    final_text=txt if t.role == "assistant" and txt else None,
                )
            )
            continue

        prev = consolidated[-1]
        if prev.role == t.role:
            if t.role == "user":
                if txt and txt != prev.content.strip():
                    prev.content = (prev.content + "\n\n" + txt).strip()
            else:
                if txt:
                    prev.final_text = txt
                    if prev.content.strip():
                        prev.content = prev.content.rstrip() + "\n\n" + txt
                    else:
                        prev.content = txt
                if t.thinking and t.thinking.strip():
                    if prev.thinking:
                        prev.thinking = prev.thinking.rstrip() + "\n" + t.thinking.strip()
                    else:
                        prev.thinking = t.thinking.strip()
                if t.tool_calls:
                    prev.tool_calls.extend(t.tool_calls)
            if t.timestamp:
                prev.timestamp = t.timestamp
        else:
            consolidated.append(
                Turn(
                    role=t.role,
                    content=txt,
                    timestamp=t.timestamp,
                    thinking=t.thinking,
                    tool_calls=list(t.tool_calls),
                    final_text=txt if t.role == "assistant" and txt else None,
                )
            )
    return consolidated


class BaseAdapter:
    """Base interface implemented by each agent platform adapter."""

    agent_name: str = "base"

    def list_sessions(self, cwd: Optional[str] = None, limit: int = 20) -> List[SessionSummary]:
        raise NotImplementedError

    def extract_bundle(
        self,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        max_turns: int = 25,
    ) -> Optional[ContextBundle]:
        raise NotImplementedError

    def inject_session(self, bundle: ContextBundle, target_cwd: Optional[str] = None) -> str:
        """Inject a ContextBundle into this platform's native session store and return the new session ID or path."""
        raise NotImplementedError

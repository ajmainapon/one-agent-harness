"""Grok Build CLI adapter (~/.grok/sessions and ~/.config/grok)."""

from __future__ import annotations

from datetime import datetime, timezone
import glob
import json
import os
from typing import Any, Dict, List, Optional
import uuid

from harness.adapters import (
    BaseAdapter,
    consolidate_turns,
    is_meta_handoff_text,
    normalize_path,
    paths_match,
)
from harness.models import ContextBundle, SessionSummary, Turn, inspect_git_state
from harness.platform_utils import get_default_agent_homes


class GrokAdapter(BaseAdapter):
    """Adapter for reading and injecting Grok Build CLI sessions."""

    agent_name = "grok"

    def __init__(self, grok_home: Optional[str] = None) -> None:
        self.grok_home = os.path.expanduser(grok_home or get_default_agent_homes()["grok"])
        self.sessions_dir = os.path.join(self.grok_home, "sessions")
        self._summary_cache: Dict[str, tuple[int, int, Optional[SessionSummary]]] = {}

    def _iter_session_files(self) -> List[str]:
        candidates: List[str] = []
        for base in [
            self.sessions_dir,
            os.path.expanduser("~/.config/grok/sessions"),
            os.path.expanduser("~/.xai/sessions"),
        ]:
            if os.path.isdir(base):
                candidates.extend(glob.glob(os.path.join(base, "*.jsonl")))
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return candidates

    def _quick_scan(self, fpath: str) -> Optional[SessionSummary]:
        try:
            st = os.stat(fpath)
            mtime = st.st_mtime
            cache_key = (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

        cached = self._summary_cache.get(fpath)
        if cached and cached[0] == cache_key[0] and cached[1] == cache_key[1]:
            return cached[2]

        sid = os.path.splitext(os.path.basename(fpath))[0]
        session_cwd = ""
        title = ""
        user_prompts: List[str] = []
        has_non_meta_user = False
        has_assistant = False

        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as fp:
                for line_idx, line in enumerate(fp):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not session_cwd and obj.get("cwd"):
                        session_cwd = obj["cwd"]
                    if obj.get("title") and not title:
                        title = obj["title"]
                    role = obj.get("role") or obj.get("message", {}).get("role")
                    content = str(obj.get("content") or obj.get("message", {}).get("content") or "").strip()
                    if role == "user" and content:
                        user_prompts.append(content)
                        if not is_meta_handoff_text(content):
                            has_non_meta_user = True
                        if not title:
                            title = content[:100].replace("\n", " ")
                    elif role == "assistant" and content:
                        has_assistant = True

                    if session_cwd and has_non_meta_user and has_assistant:
                        break
                    if line_idx >= 120 and (session_cwd or user_prompts or has_assistant):
                        break
        except OSError:
            return None

        if not user_prompts and not title and not has_assistant:
            self._summary_cache[fpath] = (cache_key[0], cache_key[1], None)
            return None

        is_meta = bool(user_prompts) and not has_non_meta_user
        turn_count = len(user_prompts) * 2 if has_assistant else len(user_prompts)
        updated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = SessionSummary(
            agent="grok",
            session_id=sid,
            title=title or f"Grok session {sid[:8]}",
            cwd=session_cwd,
            updated_at=updated_at,
            mtime=mtime,
            source_path=fpath,
            turn_count=max(turn_count, 1),
            is_meta_session=is_meta,
        )
        self._summary_cache[fpath] = (cache_key[0], cache_key[1], summary)
        return summary

    def list_sessions(self, cwd: Optional[str] = None, limit: Optional[int] = 20) -> List[SessionSummary]:
        results: List[SessionSummary] = []
        for fpath in self._iter_session_files():
            summary = self._quick_scan(fpath)
            if not summary:
                continue
            if cwd and not paths_match(cwd, summary.cwd):
                continue
            results.append(summary)
            if limit is not None and len(results) >= limit:
                break
        return results

    def extract_bundle(
        self,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        max_turns: int = 25,
    ) -> Optional[ContextBundle]:
        target_file: Optional[str] = None
        if session_id:
            for fpath in self._iter_session_files():
                if os.path.splitext(os.path.basename(fpath))[0].startswith(session_id):
                    target_file = fpath
                    break
        else:
            sessions = [s for s in self.list_sessions(cwd=cwd, limit=10) if not s.is_meta_session]
            if not sessions and cwd:
                sessions = [s for s in self.list_sessions(cwd=None, limit=10) if not s.is_meta_session]
            if not sessions:
                sessions = self.list_sessions(cwd=None, limit=1)
            if sessions:
                target_file = sessions[0].source_path

        if not target_file or not os.path.isfile(target_file):
            return None

        sid = os.path.splitext(os.path.basename(target_file))[0]
        mtime = os.path.getmtime(target_file)
        updated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        session_cwd = ""
        title = ""
        raw_turns: List[Turn] = []
        files_modified: List[str] = []
        files_read: List[str] = []
        recent_commands: List[str] = []

        with open(target_file, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("cwd") and not session_cwd:
                    session_cwd = obj["cwd"]
                if obj.get("title") and not title:
                    title = obj["title"]
                role = obj.get("role") or obj.get("message", {}).get("role")
                content = str(obj.get("content") or obj.get("message", {}).get("content") or "").strip()
                if role in ("user", "assistant") and content:
                    raw_turns.append(
                        Turn(
                            role=role,
                            content=content,
                            timestamp=obj.get("timestamp"),
                            thinking=obj.get("thinking"),
                            tool_calls=obj.get("tool_calls") or [],
                        )
                    )
                    if role == "user" and not title:
                        title = content[:90].replace("\n", " ")

        consolidated = consolidate_turns(raw_turns)
        all_user_turns = [t.content for t in consolidated if t.role == "user" and t.content.strip()]
        initial_goal = all_user_turns[0] if all_user_turns else ""
        latest_user_req = all_user_turns[-1] if all_user_turns else ""

        effective_cwd = session_cwd or cwd or os.getcwd()
        git_info = inspect_git_state(effective_cwd)

        bundle = ContextBundle(
            source_agent="grok",
            session_id=sid,
            title=title or f"Grok session {sid[:8]}",
            cwd=effective_cwd,
            updated_at=updated_at,
            source_path=target_file,
            git_branch=git_info["branch"],
            git_status_summary=git_info["status"] or "",
            git_diff_stat=git_info["diff_stat"] or "",
            files_modified=files_modified,
            files_read=files_read,
            recent_commands_and_errors=recent_commands,
            artifacts_and_plans=[],
            turns=consolidated[-max_turns:] if max_turns else consolidated,
            total_turn_count=len(consolidated),
            initial_goal=initial_goal,
            latest_user_request=latest_user_req,
        )
        bundle.generate_distilled_summary()
        return bundle

    def inject_session(self, bundle: ContextBundle, target_cwd: Optional[str] = None) -> str:
        """Write a Grok Build CLI session file and workspace .harness/LIVE_CONTEXT.md context file."""
        effective_cwd = normalize_path(target_cwd or bundle.cwd or os.getcwd())
        os.makedirs(self.sessions_dir, exist_ok=True)

        new_session_id = str(uuid.uuid4())
        target_file = os.path.join(self.sessions_dir, f"{new_session_id}.jsonl")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        handoff_md = bundle.to_markdown(include_full_artifacts=True, max_turns=20)
        records: List[Dict[str, Any]] = [
            {
                "session_id": new_session_id,
                "cwd": effective_cwd,
                "title": f"[Handoff:{bundle.source_agent}] {bundle.title}"[:80],
                "timestamp": now_iso,
                "role": "user",
                "content": f"[HANDOFF FROM {bundle.source_agent.upper()}]\n\n{handoff_md}",
            },
            {
                "session_id": new_session_id,
                "cwd": effective_cwd,
                "timestamp": now_iso,
                "role": "assistant",
                "content": f"Loaded full handoff context from `{bundle.source_agent}` session (\"{bundle.title}\").",
            },
        ]

        with open(target_file, "w", encoding="utf-8") as fp:
            for rec in records:
                fp.write(json.dumps(rec) + "\n")

        if effective_cwd and effective_cwd != "/" and os.access(effective_cwd, os.W_OK):
            harness_dir = os.path.join(effective_cwd, ".harness")
            os.makedirs(harness_dir, exist_ok=True)
            with open(os.path.join(harness_dir, "LIVE_CONTEXT.md"), "w", encoding="utf-8") as fp:
                fp.write(handoff_md)

        return new_session_id

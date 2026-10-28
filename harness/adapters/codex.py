"""Codex adapter (~/.codex/sessions, session_index.jsonl, and state_5.sqlite)."""

from __future__ import annotations

from datetime import datetime, timezone
import glob
import html
import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional
import uuid

from harness.adapters import (
    BaseAdapter,
    consolidate_turns,
    is_meta_handoff_text,
    normalize_path,
    parse_iso_ts,
    paths_match,
)
from harness.models import ContextBundle, SessionSummary, Turn, inspect_git_state
from harness.platform_utils import get_default_agent_homes, sqlite_ro_connect


class CodexAdapter(BaseAdapter):
    """Adapter for reading and injecting OpenAI Codex CLI/Desktop sessions."""

    agent_name = "codex"

    def __init__(self, codex_home: Optional[str] = None) -> None:
        self.codex_home = os.path.expanduser(codex_home or get_default_agent_homes()["codex"])
        self.sessions_dir = os.path.join(self.codex_home, "sessions")
        self.index_file = os.path.join(self.codex_home, "session_index.jsonl")
        self.state_db = os.path.join(self.codex_home, "state_5.sqlite")
        self._thread_meta_cache: Optional[tuple[tuple[int, int, int, int], Dict[str, Dict[str, Any]]]] = None
        self._summary_cache: Dict[str, tuple[int, int, Optional[SessionSummary]]] = {}

    def _load_thread_meta(self) -> Dict[str, Dict[str, Any]]:
        idx_sig = (0, 0)
        db_sig = (0, 0)
        try:
            st_i = os.stat(self.index_file)
            idx_sig = (st_i.st_mtime_ns, st_i.st_size)
        except OSError:
            pass
        try:
            st_d = os.stat(self.state_db)
            db_sig = (st_d.st_mtime_ns, st_d.st_size)
        except OSError:
            pass

        combined_sig = (idx_sig[0], idx_sig[1], db_sig[0], db_sig[1])
        if self._thread_meta_cache and self._thread_meta_cache[0] == combined_sig:
            return self._thread_meta_cache[1]

        meta: Dict[str, Dict[str, Any]] = {}
        # 1. Read session_index.jsonl
        if idx_sig != (0, 0):
            try:
                with open(self.index_file, "r", encoding="utf-8", errors="replace") as fp:
                    for line in fp:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if obj.get("id") and obj.get("thread_name"):
                                meta[obj["id"]] = {"title": html.unescape(str(obj["thread_name"])).strip()}
                        except json.JSONDecodeError:
                            continue
            except OSError:
                pass

        # 2. Read state_5.sqlite threads table if available
        if db_sig != (0, 0):
            try:
                conn = sqlite_ro_connect(self.state_db, timeout=2.0)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, name, title, cwd, git_branch, thread_source, agent_path FROM threads"
                ).fetchall()
                for r in rows:
                    tid = r["id"]
                    entry = meta.setdefault(tid, {})
                    t_title = r["name"] or r["title"] or entry.get("title") or ""
                    if t_title:
                        entry["title"] = html.unescape(str(t_title)).strip()
                    if r["cwd"]:
                        entry["cwd"] = str(r["cwd"])
                    if r["git_branch"]:
                        entry["git_branch"] = str(r["git_branch"])
                    tsrc = str(r["thread_source"] or "")
                    apath = str(r["agent_path"] or "")
                    if tsrc in ("guardian_review", "subagent") or (apath and apath != "/root"):
                        entry["is_internal"] = True
                conn.close()
            except Exception:
                pass
        self._thread_meta_cache = (combined_sig, meta)
        self._summary_cache.clear()
        return meta

    def _iter_rollout_files(self) -> List[str]:
        if not os.path.isdir(self.sessions_dir):
            return []
        pattern = os.path.join(self.sessions_dir, "*", "*", "*", "*.jsonl")
        files = glob.glob(pattern)
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return files

    @staticmethod
    def _is_boilerplate_text(text: str) -> bool:
        t = text.strip()
        boilerplate_prefixes = (
            "<environment_context>",
            "<skills_instructions>",
            "<multi_agent_role>",
            "<multi_agent_mode>",
            "<permissions",
            "<app-context>",
            "<collaboration_mode>",
            "<skill>",
            "<subagent_notification>",
            "[external_agent_tool_call",
            "[external_agent_tool_result]",
            "The following is the Codex agent history whose last command request is being assessed",
        )
        return any(t.startswith(p) for p in boilerplate_prefixes)

    @staticmethod
    def _is_internal_session_meta(payload: Dict[str, Any]) -> bool:
        """Return True if session_meta belongs to a guardian_review or subagent child thread."""
        tsrc = payload.get("thread_source")
        if isinstance(tsrc, dict) or tsrc in ("guardian_review", "subagent"):
            return True
        apath = payload.get("agent_path")
        if apath and apath != "/root":
            return True
        parent_id = payload.get("parent_thread_id")
        own_id = payload.get("id")
        if parent_id and own_id and parent_id != own_id:
            return True
        return False

    @classmethod
    def _extract_message_text(cls, content: Any) -> str:
        if isinstance(content, str):
            return "" if cls._is_boilerplate_text(content) else html.unescape(content.strip())
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                txt = item.get("text", "")
                if not txt or cls._is_boilerplate_text(txt):
                    continue
                m = re.search(r"<command>\s*(.*?)\s*</command>", txt, flags=re.DOTALL)
                if m:
                    parts.append(html.unescape(m.group(1).strip()))
                else:
                    parts.append(html.unescape(txt.strip()))
            return "\n".join(p for p in parts if p)
        return ""

    @staticmethod
    def _parse_codex_tool_input(tname: str, raw_in: Any) -> tuple[str, List[str], List[str]]:
        """Extract clean command summary, modified files, and read files from Codex tool calls."""
        raw_str = str(raw_in or "").strip()
        modified: List[str] = []
        read_files: List[str] = []

        for m in re.findall(r"\*\*\* (?:Update|Add) File:\s*([^\n\"\\]+)", raw_str):
            fp = m.strip()
            if fp and fp not in modified:
                modified.append(fp)

        cmds = re.findall(r'exec_command\(\s*\{\s*cmd\s*:\s*"((?:\\.|[^"\\])*)"', raw_str)
        if cmds:
            decoded_cmds: List[str] = []
            for c in cmds:
                try:
                    decoded_cmds.append(json.loads(f'"{c}"'))
                except Exception:
                    decoded_cmds.append(c)
            summary = " ; ".join(decoded_cmds).replace("\n", " ")[:180]
            return summary, modified, read_files

        if raw_str.startswith("{"):
            try:
                obj = json.loads(raw_str)
                if isinstance(obj, dict):
                    cmd_val = obj.get("cmd") or obj.get("command")
                    if isinstance(cmd_val, list):
                        cmd_val = " ".join(str(x) for x in cmd_val)
                    if cmd_val:
                        return str(cmd_val).replace("\n", " ")[:180], modified, read_files
            except Exception:
                pass

        return raw_str.replace("\n", " ")[:160], modified, read_files

    def _quick_scan(self, fpath: str, thread_meta: Dict[str, Dict[str, Any]]) -> Optional[SessionSummary]:
        try:
            st = os.stat(fpath)
            file_mtime = st.st_mtime
            cache_key = (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

        cached = self._summary_cache.get(fpath)
        if cached and cached[0] == cache_key[0] and cached[1] == cache_key[1]:
            return cached[2]

        session_id = ""
        cwd = ""
        user_prompts: List[str] = []
        has_non_meta_user = False
        has_assistant = False
        last_msg_ts = ""
        stopped_early = False

        try:
            with open(fpath, "rb") as fp:
                for line_idx, raw_line in enumerate(fp):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    t = obj.get("type")
                    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                    if t == "session_meta":
                        if self._is_internal_session_meta(payload):
                            self._summary_cache[fpath] = (cache_key[0], cache_key[1], None)
                            return None
                        session_id = payload.get("id") or payload.get("session_id") or session_id
                        cwd = payload.get("cwd") or cwd
                    elif t == "response_item":
                        if obj.get("timestamp"):
                            last_msg_ts = obj["timestamp"]
                        if payload.get("type") == "message":
                            role = payload.get("role")
                            if role == "user":
                                txt = self._extract_message_text(payload.get("content"))
                                if txt:
                                    user_prompts.append(txt)
                                    if not is_meta_handoff_text(txt):
                                        has_non_meta_user = True
                            elif role == "assistant":
                                txt = self._extract_message_text(payload.get("content"))
                                if txt:
                                    has_assistant = True

                    # Stop reading once project folder, non-meta user prompt, and assistant response are found
                    if cwd and has_non_meta_user and has_assistant:
                        stopped_early = True
                        break
                    if line_idx >= 120 and (cwd or user_prompts or has_assistant):
                        stopped_early = True
                        break

                # Read tail 8 KB to capture latest response_item timestamp if we stopped early
                if stopped_early:
                    offset = max(0, st.st_size - 8192)
                    fp.seek(offset)
                    tail_lines = fp.read().decode("utf-8", errors="replace").splitlines()
                    for tail_line in reversed(tail_lines[1:] if offset > 0 else tail_lines):
                        tail_line = tail_line.strip()
                        if not tail_line or "response_item" not in tail_line:
                            continue
                        try:
                            tail_obj = json.loads(tail_line)
                        except json.JSONDecodeError:
                            continue
                        if tail_obj.get("type") == "response_item" and tail_obj.get("timestamp"):
                            last_msg_ts = tail_obj["timestamp"]
                            break
        except OSError:
            return None

        if not session_id:
            m = re.search(
                r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                os.path.basename(fpath),
            )
            session_id = m.group(1) if m else os.path.splitext(os.path.basename(fpath))[0]

        t_info = thread_meta.get(session_id, {})
        if t_info.get("is_internal"):
            self._summary_cache[fpath] = (cache_key[0], cache_key[1], None)
            return None
        if not cwd and t_info.get("cwd"):
            cwd = t_info["cwd"]

        if not user_prompts and not t_info.get("title") and not has_assistant:
            self._summary_cache[fpath] = (cache_key[0], cache_key[1], None)
            return None

        first_user_text = user_prompts[0][:100].replace("\n", " ") if user_prompts else ""
        title = t_info.get("title") or first_user_text or f"Codex session {session_id[:8]}"
        if title.lower() == "guardian review":
            self._summary_cache[fpath] = (cache_key[0], cache_key[1], None)
            return None

        is_meta = (
            (bool(user_prompts) and not has_non_meta_user)
            or is_meta_handoff_text(title)
            or title.lower() == "load handoff context"
        )
        turn_count = len(user_prompts) * 2 if has_assistant else len(user_prompts)

        parsed_ts = parse_iso_ts(last_msg_ts)
        effective_mtime = parsed_ts if parsed_ts is not None else file_mtime
        updated_at = datetime.fromtimestamp(effective_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        summary = SessionSummary(
            agent="codex",
            session_id=session_id,
            title=title.strip(),
            cwd=cwd,
            updated_at=updated_at,
            mtime=effective_mtime,
            source_path=fpath,
            turn_count=max(turn_count, 1),
            is_meta_session=is_meta,
        )
        self._summary_cache[fpath] = (cache_key[0], cache_key[1], summary)
        return summary

    def list_sessions(self, cwd: Optional[str] = None, limit: Optional[int] = 20) -> List[SessionSummary]:
        thread_meta = self._load_thread_meta()
        results: List[SessionSummary] = []
        for fpath in self._iter_rollout_files():
            summary = self._quick_scan(fpath, thread_meta)
            if not summary:
                continue
            if cwd and not paths_match(cwd, summary.cwd):
                continue
            results.append(summary)
        results.sort(key=lambda s: s.mtime, reverse=True)
        return results[:limit]

    def extract_bundle(
        self,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        max_turns: int = 25,
    ) -> Optional[ContextBundle]:
        thread_meta = self._load_thread_meta()
        target_file: Optional[str] = None
        if session_id:
            for fpath in self._iter_rollout_files():
                if session_id in os.path.basename(fpath):
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

        mtime = os.path.getmtime(target_file)
        updated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        sid = ""
        session_cwd = ""
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

                t = obj.get("type")
                ts = obj.get("timestamp")
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

                if t == "session_meta":
                    sid = payload.get("id") or payload.get("session_id") or sid
                    session_cwd = payload.get("cwd") or session_cwd
                elif t == "compacted" and not raw_turns:
                    for item in payload.get("replacement_history") or []:
                        if isinstance(item, dict) and item.get("type") == "message":
                            role = item.get("role")
                            if role in ("user", "assistant"):
                                txt = self._extract_message_text(item.get("content"))
                                if txt:
                                    raw_turns.append(Turn(role=role, content=txt, timestamp=ts))
                elif t == "response_item":
                    ptype = payload.get("type")
                    if ptype == "message":
                        role = payload.get("role")
                        if role in ("user", "assistant"):
                            txt = self._extract_message_text(payload.get("content"))
                            if txt:
                                raw_turns.append(Turn(role=role, content=txt, timestamp=ts))
                    elif ptype in ("custom_tool_call", "function_call"):
                        tname = payload.get("name", "tool")
                        raw_in = payload.get("input") or payload.get("arguments") or ""
                        cmd_summary, mod_f, read_f = self._parse_codex_tool_input(tname, raw_in)
                        for fpath in mod_f:
                            if fpath not in files_modified:
                                files_modified.append(fpath)
                        for fpath in read_f:
                            if fpath not in files_read:
                                files_read.append(fpath)
                        if tname in ("exec", "shell", "bash", "exec_command") and cmd_summary:
                            recent_commands.append(f"`{cmd_summary}`")

                        tc_entry = {"name": tname, "summary": cmd_summary}
                        raw_turns.append(Turn(role="assistant", content="", timestamp=ts, tool_calls=[tc_entry]))
                    elif ptype in ("custom_tool_call_output", "function_call_output"):
                        out_val = payload.get("output")
                        out_str = json.dumps(out_val) if not isinstance(out_val, str) else out_val
                        if any(
                            err in out_str
                            for err in ("Operation not permitted", "Traceback (most recent call last)", "Process exited with code")
                        ):
                            if "Process exited with code 0" not in out_str:
                                clean_err = out_str.replace("\\n", " ").replace("\n", " ")[:200]
                                recent_commands.append(f"[TOOL OUTPUT/ERROR] {clean_err}")

        if not sid:
            m = re.search(
                r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                os.path.basename(target_file),
            )
            sid = m.group(1) if m else os.path.splitext(os.path.basename(target_file))[0]

        consolidated = consolidate_turns(raw_turns)
        all_user_turns = [t.content for t in consolidated if t.role == "user" and t.content.strip()]
        initial_goal = all_user_turns[0] if all_user_turns else ""
        latest_user_req = all_user_turns[-1] if all_user_turns else ""

        t_info = thread_meta.get(sid, {})
        title = (
            t_info.get("title")
            or (initial_goal[:80].replace("\n", " ") if initial_goal else f"Codex session {sid[:8]}")
        )
        effective_cwd = session_cwd or t_info.get("cwd") or cwd or os.getcwd()
        git_info = inspect_git_state(effective_cwd)

        bundle = ContextBundle(
            source_agent="codex",
            session_id=sid,
            title=title,
            cwd=effective_cwd,
            updated_at=updated_at,
            source_path=target_file,
            git_branch=t_info.get("git_branch") or git_info["branch"],
            git_status_summary=git_info["status"] or "",
            git_diff_stat=git_info["diff_stat"] or "",
            files_modified=files_modified,
            files_read=files_read,
            recent_commands_and_errors=recent_commands[-20:],
            artifacts_and_plans=[],
            turns=consolidated[-max_turns:] if max_turns else consolidated,
            total_turn_count=len(consolidated),
            initial_goal=initial_goal,
            latest_user_request=latest_user_req,
        )
        bundle.generate_distilled_summary()
        return bundle

    def inject_session(self, bundle: ContextBundle, target_cwd: Optional[str] = None) -> str:
        """Convert a ContextBundle into a native Codex rollout .jsonl file and register in session_index.jsonl & state_5.sqlite."""
        effective_cwd = normalize_path(target_cwd or bundle.cwd or os.getcwd())
        now = datetime.now(timezone.utc)
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        now_sec = int(now.timestamp())
        now_ms = int(now.timestamp() * 1000)

        date_folder = os.path.join(
            self.sessions_dir,
            now.strftime("%Y"),
            now.strftime("%m"),
            now.strftime("%d"),
        )
        os.makedirs(date_folder, exist_ok=True)

        new_session_id = str(uuid.uuid4())
        fname = f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{new_session_id}.jsonl"
        target_file = os.path.join(date_folder, fname)

        from harness.adapters import strip_handoff_prefix

        clean_title = strip_handoff_prefix(bundle.title)
        handoff_text = (
            f"[HANDOFF FROM {bundle.source_agent.upper()} — Session {bundle.session_id}]\n\n"
            f"{bundle.to_markdown(include_full_artifacts=True, max_turns=15)}"
        )
        thread_title = f"[Handoff:{bundle.source_agent}] {clean_title}"[:80]

        records = [
            {
                "timestamp": now_iso,
                "ordinal": 0,
                "type": "session_meta",
                "payload": {
                    "session_id": new_session_id,
                    "id": new_session_id,
                    "timestamp": now_iso,
                    "cwd": effective_cwd,
                    "runtime_workspace_roots": [effective_cwd],
                    "originator": "codex-tui",
                    "source": "cli",
                    "thread_source": "user",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": now_iso,
                "ordinal": 1,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": f"msg_{uuid.uuid4().hex[:24]}",
                    "role": "user",
                    "content": [{"type": "input_text", "text": handoff_text}],
                },
            },
            {
                "timestamp": now_iso,
                "ordinal": 2,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": f"msg_{uuid.uuid4().hex[:24]}",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                f"Loaded full handoff context from `{bundle.source_agent}` session "
                                f"(\"{bundle.title}\"). Ready to continue."
                            ),
                        }
                    ],
                },
            },
        ]

        with open(target_file, "w", encoding="utf-8") as fp:
            for rec in records:
                fp.write(json.dumps(rec) + "\n")

        os.makedirs(self.codex_home, exist_ok=True)
        index_entry = {
            "id": new_session_id,
            "thread_name": thread_title,
            "updated_at": now_iso,
        }
        with open(self.index_file, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(index_entry) + "\n")

        if os.path.isfile(self.state_db):
            try:
                conn = sqlite3.connect(self.state_db, timeout=3.0)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO threads (
                        id, rollout_path, created_at, updated_at, source, model_provider,
                        cwd, title, sandbox_policy, approval_mode, tokens_used,
                        has_user_event, archived, cli_version, first_user_message,
                        created_at_ms, updated_at_ms, thread_source, preview,
                        recency_at, recency_at_ms, history_mode, name, is_pinned, originator
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_session_id,
                        target_file,
                        now_sec,
                        now_sec,
                        "cli",
                        "openai",
                        effective_cwd,
                        thread_title,
                        '{"type":"workspace-write"}',
                        "on-request",
                        0,
                        1,
                        0,
                        "0.162.1",
                        thread_title,
                        now_ms,
                        now_ms,
                        "user",
                        thread_title,
                        now_sec,
                        now_ms,
                        "paginated",
                        thread_title,
                        0,
                        "codex-tui",
                    ),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

        return new_session_id

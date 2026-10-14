"""Antigravity adapter (~/.gemini/antigravity/brain/<conversation-id> & conversation_summaries.db)."""

from __future__ import annotations

from datetime import datetime, timezone
import glob
import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

from harness.adapters import (
    BaseAdapter,
    consolidate_turns,
    is_meta_handoff_text,
    normalize_path,
    paths_match,
)
from harness.models import ArtifactDoc, ContextBundle, SessionSummary, Turn, inspect_git_state
from harness.platform_utils import get_default_agent_homes, sqlite_ro_connect


def _clean_arg_val(val: Any) -> str:
    """Unwrap JSON-encoded string values inside Antigravity tool_calls.args."""
    if val is None:
        return ""
    if isinstance(val, str):
        s = val.strip()
        if s.startswith('"') and s.endswith('"'):
            try:
                return str(json.loads(s))
            except Exception:
                return s.strip('"')
        return s
    return str(val)


def _clean_user_request(raw: str) -> str:
    """Extract clean user prompt text from Antigravity USER_INPUT steps."""
    if not raw:
        return ""
    # Strip <ADDITIONAL_METADATA> and <USER_SETTINGS_CHANGE> blocks first
    cleaned = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", raw, flags=re.DOTALL)
    cleaned = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", cleaned, flags=re.DOTALL)

    m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", cleaned, flags=re.DOTALL)
    if m:
        inner = m.group(1).strip()
        prefix = cleaned[: m.start()].strip()
        if inner and prefix and "Comments on artifact" in prefix:
            return f"{prefix}\n\n{inner}"
        if inner:
            return inner
        if prefix:
            return prefix

    cleaned = re.sub(r"</?USER_REQUEST>", "", cleaned).strip()
    return cleaned


class AntigravityAdapter(BaseAdapter):
    """Adapter for reading and injecting Antigravity conversations and artifacts."""

    agent_name = "antigravity"

    def __init__(self, agy_home: Optional[str] = None) -> None:
        self.agy_home = os.path.expanduser(agy_home or get_default_agent_homes()["antigravity"])
        self.brain_dir = os.path.join(self.agy_home, "brain")
        self.summaries_db = os.path.join(self.agy_home, "conversation_summaries.db")

    def _load_db_metadata(self) -> Dict[str, Dict[str, Any]]:
        """Read titles, workspace_uris, and subagent flags from conversation_summaries.db."""
        meta: Dict[str, Dict[str, Any]] = {}
        if not os.path.isfile(self.summaries_db):
            return meta
        try:
            conn = sqlite_ro_connect(self.summaries_db, timeout=2.0)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT conversation_id, title, preview, workspace_uris, parent_conversation_id, nesting_depth "
                "FROM conversation_summaries"
            ).fetchall()
            for r in rows:
                cid = r["conversation_id"]
                w_cwd = ""
                raw_uris = r["workspace_uris"]
                if raw_uris:
                    try:
                        uris = json.loads(raw_uris)
                        if isinstance(uris, list) and uris:
                            w_cwd = normalize_path(str(uris[0]))
                    except Exception:
                        pass
                meta[cid] = {
                    "title": (r["title"] or r["preview"] or "").strip(),
                    "cwd": w_cwd,
                    "is_subagent": bool(r["parent_conversation_id"]) or int(r["nesting_depth"] or 0) > 0,
                }
            conn.close()
        except Exception:
            pass
        return meta

    def _iter_transcript_files(self) -> List[str]:
        if not os.path.isdir(self.brain_dir):
            return []
        pattern = os.path.join(self.brain_dir, "*", ".system_generated", "logs", "transcript.jsonl")
        files = glob.glob(pattern)
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return files

    def _session_id_from_transcript(self, fpath: str) -> str:
        parts = os.path.normpath(fpath).split(os.sep)
        if len(parts) >= 4:
            return parts[-4]
        return os.path.basename(os.path.dirname(fpath))

    @staticmethod
    def _iter_transcript_records(fpath: str) -> List[Dict[str, Any]]:
        """Read transcript.jsonl, resolving any lines with truncated_fields from transcript_full.jsonl."""
        full_path = os.path.join(os.path.dirname(fpath), "transcript_full.jsonl")
        full_lines: Optional[List[str]] = None

        records: List[Dict[str, Any]] = []
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

                    if obj.get("truncated_fields") and os.path.isfile(full_path):
                        if full_lines is None:
                            try:
                                with open(full_path, "r", encoding="utf-8", errors="replace") as full_fp:
                                    full_lines = [l.strip() for l in full_fp]
                            except OSError:
                                full_lines = []
                        if full_lines and line_idx < len(full_lines) and full_lines[line_idx]:
                            try:
                                full_obj = json.loads(full_lines[line_idx])
                                if full_obj.get("step_index") == obj.get("step_index"):
                                    obj = full_obj
                            except json.JSONDecodeError:
                                pass
                    records.append(obj)
        except OSError:
            pass
        return records

    @staticmethod
    def _extract_cwd_from_tool_args(clean_args: Dict[str, str]) -> str:
        for key in ("Cwd", "DirectoryPath", "SearchPath"):
            val = clean_args.get(key, "")
            if val and "/.gemini/antigravity/" not in val:
                return normalize_path(val)
        for key in ("TargetFile", "AbsolutePath", "File"):
            val = clean_args.get(key, "")
            if val and "/.gemini/antigravity/" not in val:
                parent = os.path.dirname(val)
                if parent and parent != "/":
                    return normalize_path(parent)
        return ""

    def _quick_scan(self, fpath: str, db_meta: Dict[str, Dict[str, Any]]) -> Optional[SessionSummary]:
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            return None

        session_id = self._session_id_from_transcript(fpath)
        info = db_meta.get(session_id, {})
        if info.get("is_subagent"):
            return None

        cwd = info.get("cwd", "")
        db_title = info.get("title", "")
        first_user_title = ""
        user_prompts: List[str] = []
        has_planner = False

        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    stype = obj.get("type")
                    if stype == "USER_INPUT":
                        txt = _clean_user_request(obj.get("content", ""))
                        if txt:
                            user_prompts.append(txt)
                            if not first_user_title:
                                first_user_title = txt[:100].replace("\n", " ")
                    elif stype == "PLANNER_RESPONSE":
                        has_planner = True
                        if not cwd:
                            for tc in obj.get("tool_calls") or []:
                                raw_args = tc.get("args") or {}
                                clean_args = {k: _clean_arg_val(v) for k, v in raw_args.items()}
                                candidate_cwd = self._extract_cwd_from_tool_args(clean_args)
                                if candidate_cwd:
                                    cwd = candidate_cwd
                                    break
        except OSError:
            return None

        # Skip subagent transcripts that have zero USER_INPUT steps
        if not user_prompts:
            return None

        title = db_title or first_user_title or f"Antigravity session {session_id[:8]}"
        is_meta = all(is_meta_handoff_text(p) for p in user_prompts)
        turn_count = len(user_prompts) * 2 if has_planner else len(user_prompts)
        updated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        return SessionSummary(
            agent="antigravity",
            session_id=session_id,
            title=title.strip(),
            cwd=cwd,
            updated_at=updated_at,
            mtime=mtime,
            source_path=fpath,
            turn_count=max(turn_count, 1),
            is_meta_session=is_meta,
        )

    def list_sessions(self, cwd: Optional[str] = None, limit: int = 20) -> List[SessionSummary]:
        db_meta = self._load_db_metadata()
        results: List[SessionSummary] = []
        for fpath in self._iter_transcript_files():
            summary = self._quick_scan(fpath, db_meta)
            if not summary:
                continue
            if cwd and not paths_match(cwd, summary.cwd):
                continue
            results.append(summary)
            if len(results) >= limit:
                break
        return results

    def extract_bundle(
        self,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        max_turns: int = 25,
    ) -> Optional[ContextBundle]:
        db_meta = self._load_db_metadata()
        target_file: Optional[str] = None
        if session_id:
            for fpath in self._iter_transcript_files():
                sid = self._session_id_from_transcript(fpath)
                if sid.startswith(session_id):
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

        sid = self._session_id_from_transcript(target_file)
        mtime = os.path.getmtime(target_file)
        updated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        info = db_meta.get(sid, {})
        session_cwd = info.get("cwd", "")
        title = info.get("title", "")
        raw_turns: List[Turn] = []
        files_modified: List[str] = []
        files_read: List[str] = []
        recent_commands: List[str] = []

        for obj in self._iter_transcript_records(target_file):
            stype = obj.get("type")
            ts = obj.get("created_at")
            if stype == "USER_INPUT":
                txt = _clean_user_request(obj.get("content", ""))
                if txt:
                    raw_turns.append(Turn(role="user", content=txt, timestamp=ts))
                    if not title:
                        title = txt[:90].replace("\n", " ")
            elif stype == "PLANNER_RESPONSE":
                content = (obj.get("content") or "").strip()
                thinking = (obj.get("thinking") or "").strip()
                raw_tcs = obj.get("tool_calls") or []
                parsed_tcs: List[Dict[str, Any]] = []

                for tc in raw_tcs:
                    tname = tc.get("name", "tool")
                    args = tc.get("args") or {}
                    clean_args = {k: _clean_arg_val(v) for k, v in args.items()}

                    if not session_cwd:
                        session_cwd = self._extract_cwd_from_tool_args(clean_args)

                    summary_val = (
                        clean_args.get("TargetFile")
                        or clean_args.get("AbsolutePath")
                        or clean_args.get("CommandLine")
                        or clean_args.get("DirectoryPath")
                        or clean_args.get("toolSummary")
                        or ""
                    )
                    parsed_tcs.append(
                        {
                            "name": tname,
                            "input": clean_args,
                            "summary": summary_val[:120],
                        }
                    )

                    if tname in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
                        tf = clean_args.get("TargetFile", "")
                        if tf and tf not in files_modified:
                            files_modified.append(tf)
                    elif tname in ("view_file", "view_file_outline", "view_code_item"):
                        ap = clean_args.get("AbsolutePath") or clean_args.get("File") or ""
                        if ap and ap not in files_read:
                            files_read.append(ap)
                    elif tname == "run_command" and clean_args.get("CommandLine"):
                        cmd_str = clean_args["CommandLine"].replace("\n", " ")[:160]
                        recent_commands.append(f"`{cmd_str}`")

                if content or thinking or parsed_tcs:
                    raw_turns.append(
                        Turn(
                            role="assistant",
                            content=content,
                            timestamp=ts,
                            thinking=thinking or None,
                            tool_calls=parsed_tcs,
                        )
                    )
            elif stype == "GENERIC":
                gcontent = obj.get("content") or ""
                if "The command exited with code" in gcontent and "code 0." not in gcontent:
                    err_snip = gcontent.strip().replace("\n", " ")[:220]
                    recent_commands.append(f"[COMMAND ERROR] {err_snip}")

        consolidated = consolidate_turns(raw_turns)
        all_user_turns = [t.content for t in consolidated if t.role == "user" and t.content.strip()]
        initial_goal = all_user_turns[0] if all_user_turns else ""
        latest_user_req = all_user_turns[-1] if all_user_turns else ""

        # Collect markdown artifacts from the brain directory
        brain_conv_dir = os.path.join(self.brain_dir, sid)
        artifacts: List[ArtifactDoc] = []
        if os.path.isdir(brain_conv_dir):
            for md_file in sorted(glob.glob(os.path.join(brain_conv_dir, "*.md"))):
                bname = os.path.basename(md_file)
                if bname.endswith(".resolved") or bname == "HANDOFF_CONTEXT.md":
                    continue
                try:
                    with open(md_file, "r", encoding="utf-8", errors="replace") as mdfp:
                        artifacts.append(
                            ArtifactDoc(
                                name=bname,
                                path=md_file,
                                content=mdfp.read(),
                            )
                        )
                except OSError:
                    pass

        effective_cwd = session_cwd or cwd or os.getcwd()
        git_info = inspect_git_state(effective_cwd)

        bundle = ContextBundle(
            source_agent="antigravity",
            session_id=sid,
            title=title or f"Antigravity session {sid[:8]}",
            cwd=effective_cwd,
            updated_at=updated_at,
            source_path=target_file,
            git_branch=git_info["branch"],
            git_status_summary=git_info["status"] or "",
            git_diff_stat=git_info["diff_stat"] or "",
            files_modified=files_modified,
            files_read=files_read,
            recent_commands_and_errors=recent_commands[-20:],
            artifacts_and_plans=artifacts,
            turns=consolidated[-max_turns:] if max_turns else consolidated,
            total_turn_count=len(consolidated),
            initial_goal=initial_goal,
            latest_user_request=latest_user_req,
        )
        bundle.generate_distilled_summary()
        return bundle

    def inject_session(self, bundle: ContextBundle, target_cwd: Optional[str] = None) -> str:
        """Write the handoff context into the most recent Antigravity brain folder and workspace .harness/LIVE_CONTEXT.md."""
        effective_cwd = normalize_path(target_cwd or bundle.cwd or os.getcwd())
        if not effective_cwd or effective_cwd == "/" or not os.access(effective_cwd, os.W_OK):
            effective_cwd = os.path.expanduser("~/.harness")
        harness_dir = os.path.join(effective_cwd, ".harness") if not effective_cwd.endswith("/.harness") else effective_cwd
        os.makedirs(harness_dir, exist_ok=True)
        live_path = os.path.join(harness_dir, "LIVE_CONTEXT.md")
        md_content = bundle.to_markdown(include_full_artifacts=True, max_turns=25)
        with open(live_path, "w", encoding="utf-8") as fp:
            fp.write(md_content)

        sessions = self.list_sessions(limit=1)
        if sessions:
            latest_sid = sessions[0].session_id
            conv_dir = os.path.join(self.brain_dir, latest_sid)
            if os.path.isdir(conv_dir):
                handoff_art = os.path.join(conv_dir, "HANDOFF_CONTEXT.md")
                with open(handoff_art, "w", encoding="utf-8") as fp:
                    fp.write(md_content)
        return live_path

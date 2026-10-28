"""Claude Code adapter (~/.claude/projects and ~/.claude/plans)."""

from __future__ import annotations

from datetime import datetime, timezone
import glob
import json
import os
import re
from typing import Any, Dict, List, Optional
import uuid

from harness.adapters import (
    BaseAdapter,
    consolidate_turns,
    is_meta_handoff_text,
    normalize_path,
    paths_match,
)
from harness.models import ArtifactDoc, ContextBundle, SessionSummary, Turn, inspect_git_state
from harness.platform_utils import get_default_agent_homes


def encode_claude_project_dir(cwd: str) -> str:
    """Encode an absolute directory path into Claude Code's project folder name."""
    norm = normalize_path(cwd)
    return re.sub(r"[^a-zA-Z0-9]", "-", norm)


class ClaudeAdapter(BaseAdapter):
    """Adapter for reading and injecting Claude Code sessions."""

    agent_name = "claude"

    def __init__(self, claude_home: Optional[str] = None) -> None:
        self.claude_home = os.path.expanduser(claude_home or get_default_agent_homes()["claude"])
        self.projects_dir = os.path.join(self.claude_home, "projects")
        self.plans_dir = os.path.join(self.claude_home, "plans")

    def _iter_jsonl_files(self) -> List[str]:
        if not os.path.isdir(self.projects_dir):
            return []
        pattern = os.path.join(self.projects_dir, "*", "*.jsonl")
        files = [f for f in glob.glob(pattern) if not os.path.basename(f).startswith("agent-")]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return files

    @classmethod
    def _clean_user_str(cls, raw: str) -> str:
        s = raw.strip()
        if not s:
            return ""
        # Skip Claude CLI meta command outputs/inputs
        meta_tags = (
            "<local-command-caveat>",
            "<command-name>",
            "<local-command-stdout>",
            "<local-command-stderr>",
            "<bash-notification>",
        )
        if any(tag in s for tag in meta_tags):
            return ""
        # Unwrap <pasted_content ...>...</pasted_content>
        s = re.sub(r"</?pasted_content[^>]*>", "", s).strip()
        if s.lower() in ("/exit", "/quit", "/clear", "/compact", "/help", "/status", "/model", "/login", "/logout", "/resume"):
            return ""
        return s

    @classmethod
    def _extract_user_text(cls, content: Any) -> str:
        if isinstance(content, str):
            return cls._clean_user_str(content)
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    cleaned = cls._clean_user_str(item.get("text", ""))
                    if cleaned:
                        parts.append(cleaned)
            return "\n".join(parts)
        return ""

    @staticmethod
    def _extract_assistant_parts(content: Any) -> tuple[str, str, List[Dict[str, Any]]]:
        text_parts: List[str] = []
        thinking_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []

        if isinstance(content, str):
            return content.strip(), "", []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                btype = item.get("type")
                if btype == "text" and item.get("text"):
                    text_parts.append(item["text"].strip())
                elif btype == "thinking" and item.get("thinking"):
                    thinking_parts.append(item["thinking"].strip())
                elif btype == "tool_use":
                    tname = item.get("name", "tool")
                    tinput = item.get("input", {}) if isinstance(item.get("input"), dict) else {}
                    summary = (
                        tinput.get("file_path")
                        or tinput.get("path")
                        or tinput.get("command")
                        or tinput.get("pattern")
                        or ""
                    )
                    tool_calls.append(
                        {
                            "id": item.get("id", ""),
                            "name": tname,
                            "input": tinput,
                            "summary": str(summary)[:120],
                        }
                    )
        return "\n\n".join(text_parts), "\n".join(thinking_parts), tool_calls

    def _quick_scan_file(self, fpath: str) -> Optional[SessionSummary]:
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            return None

        session_id = os.path.splitext(os.path.basename(fpath))[0]
        cwd = ""
        title = ""
        user_prompts: List[str] = []
        has_assistant = False

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

                    t = obj.get("type")
                    if not cwd and obj.get("cwd"):
                        cwd = obj["cwd"]
                    if t == "ai-title" and obj.get("aiTitle"):
                        title = obj["aiTitle"]
                    elif t == "last-prompt" and obj.get("lastPrompt") and not title:
                        title = obj["lastPrompt"][:100].replace("\n", " ")
                    elif t == "user" and not obj.get("isMeta"):
                        msg = obj.get("message", {})
                        text = self._extract_user_text(msg.get("content"))
                        if text:
                            user_prompts.append(text)
                    elif t == "assistant":
                        has_assistant = True
        except OSError:
            return None

        if not user_prompts and not title and not has_assistant:
            return None

        first_user_text = user_prompts[0][:100].replace("\n", " ") if user_prompts else ""
        display_title = title or first_user_text or f"Claude session {session_id[:8]}"
        updated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        is_meta = bool(user_prompts) and all(is_meta_handoff_text(p) for p in user_prompts)
        turn_count = len(user_prompts) * 2 if has_assistant else len(user_prompts)

        return SessionSummary(
            agent="claude",
            session_id=session_id,
            title=display_title.strip(),
            cwd=cwd,
            updated_at=updated_at,
            mtime=mtime,
            source_path=fpath,
            turn_count=max(turn_count, 1),
            is_meta_session=is_meta,
        )

    def list_sessions(self, cwd: Optional[str] = None, limit: Optional[int] = 20) -> List[SessionSummary]:
        results: List[SessionSummary] = []
        for fpath in self._iter_jsonl_files():
            summary = self._quick_scan_file(fpath)
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
            for fpath in self._iter_jsonl_files():
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
        git_branch: Optional[str] = None
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
                if obj.get("gitBranch") and not git_branch:
                    git_branch = obj["gitBranch"]

                t = obj.get("type")
                ts = obj.get("timestamp")
                if t == "ai-title" and obj.get("aiTitle"):
                    title = obj["aiTitle"]
                elif t == "user" and not obj.get("isMeta"):
                    msg = obj.get("message", {})
                    content = msg.get("content")
                    user_text = self._extract_user_text(content)
                    if user_text:
                        raw_turns.append(Turn(role="user", content=user_text, timestamp=ts))
                        if not title:
                            title = user_text[:90].replace("\n", " ")
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "tool_result" and item.get("is_error"):
                                err_txt = str(item.get("content", ""))[:200].replace("\n", " ")
                                recent_commands.append(f"[ERROR] {err_txt}")
                elif t == "assistant":
                    msg = obj.get("message", {})
                    text, thinking, tool_calls = self._extract_assistant_parts(msg.get("content"))
                    for tc in tool_calls:
                        tname = tc["name"]
                        tinput = tc["input"]
                        fpath = tinput.get("file_path") or tinput.get("path")
                        if tname in ("Edit", "Write", "MultiEdit", "NotebookEdit") and fpath:
                            if fpath not in files_modified:
                                files_modified.append(fpath)
                        elif tname in ("Read", "Glob", "Grep", "LS") and fpath:
                            if fpath not in files_read:
                                files_read.append(fpath)
                        elif tname == "Bash" and tinput.get("command"):
                            cmd_str = str(tinput["command"]).strip().replace("\n", " ")[:160]
                            recent_commands.append(f"`{cmd_str}`")

                    if text or thinking or tool_calls:
                        raw_turns.append(
                            Turn(
                                role="assistant",
                                content=text,
                                timestamp=ts,
                                thinking=thinking or None,
                                tool_calls=tool_calls,
                            )
                        )

        consolidated = consolidate_turns(raw_turns)
        all_user_turns = [t.content for t in consolidated if t.role == "user" and t.content.strip()]
        initial_goal = all_user_turns[0] if all_user_turns else ""
        latest_user_req = all_user_turns[-1] if all_user_turns else ""

        # Only attach plans explicitly touched by this session. Modification time
        # alone cannot associate a global plans directory with a project.
        artifacts: List[ArtifactDoc] = []
        if os.path.isdir(self.plans_dir):
            plan_files = sorted(
                glob.glob(os.path.join(self.plans_dir, "*.md")),
                key=os.path.getmtime,
                reverse=True,
            )
            touched_paths = {normalize_path(p) for p in files_modified + files_read}
            for pf in plan_files:
                if normalize_path(pf) in touched_paths:
                    try:
                        with open(pf, "r", encoding="utf-8", errors="replace") as pfp:
                            artifacts.append(
                                ArtifactDoc(
                                    name=os.path.basename(pf),
                                    path=pf,
                                    content=pfp.read(),
                                )
                            )
                    except OSError:
                        pass

        effective_cwd = session_cwd or cwd or os.getcwd()
        git_info = inspect_git_state(effective_cwd)

        bundle = ContextBundle(
            source_agent="claude",
            session_id=sid,
            title=title or f"Claude session {sid[:8]}",
            cwd=effective_cwd,
            updated_at=updated_at,
            source_path=target_file,
            git_branch=git_branch or git_info["branch"],
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
        """Convert a ContextBundle into a native Claude Code session .jsonl file and return the new session UUID."""
        effective_cwd = normalize_path(target_cwd or bundle.cwd or os.getcwd())
        proj_folder = encode_claude_project_dir(effective_cwd)
        target_dir = os.path.join(self.projects_dir, proj_folder)
        os.makedirs(target_dir, exist_ok=True)

        new_session_id = str(uuid.uuid4())
        target_file = os.path.join(target_dir, f"{new_session_id}.jsonl")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        records: List[Dict[str, Any]] = []
        parent_uuid: Optional[str] = None

        handoff_header = (
            f"[HANDOFF FROM {bundle.source_agent.upper()} — Session {bundle.session_id}]\n\n"
            f"{bundle.to_markdown(include_full_artifacts=True, max_turns=15)}"
        )
        seed_user_uuid = str(uuid.uuid4())
        records.append(
            {
                "parentUuid": parent_uuid,
                "isSidechain": False,
                "promptId": str(uuid.uuid4()),
                "type": "user",
                "message": {"role": "user", "content": handoff_header},
                "isMeta": False,
                "uuid": seed_user_uuid,
                "timestamp": now_iso,
                "userType": "external",
                "entrypoint": "cli",
                "cwd": effective_cwd,
                "sessionId": new_session_id,
                "gitBranch": bundle.git_branch or "main",
            }
        )
        parent_uuid = seed_user_uuid

        ack_uuid = str(uuid.uuid4())
        records.append(
            {
                "parentUuid": parent_uuid,
                "isSidechain": False,
                "type": "assistant",
                "message": {
                    "id": f"msg_{uuid.uuid4().hex[:20]}",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"I have loaded the complete context from your `{bundle.source_agent}` session "
                                f"(\"{bundle.title}\"), including all recent turns, plans, modified files, and git state. "
                                f"How would you like to proceed?"
                            ),
                        }
                    ],
                },
                "uuid": ack_uuid,
                "timestamp": now_iso,
                "userType": "external",
                "entrypoint": "cli",
                "cwd": effective_cwd,
                "sessionId": new_session_id,
                "gitBranch": bundle.git_branch or "main",
            }
        )
        parent_uuid = ack_uuid

        from harness.adapters import strip_handoff_prefix

        clean_title = strip_handoff_prefix(bundle.title)
        records.append(
            {
                "type": "ai-title",
                "aiTitle": f"[Handoff:{bundle.source_agent}] {clean_title}"[:80],
                "sessionId": new_session_id,
            }
        )
        records.append(
            {
                "type": "last-prompt",
                "lastPrompt": f"[Handoff from {bundle.source_agent}] {clean_title}"[:120],
                "leafUuid": parent_uuid,
                "sessionId": new_session_id,
            }
        )

        with open(target_file, "w", encoding="utf-8") as fp:
            for rec in records:
                fp.write(json.dumps(rec) + "\n")

        return new_session_id

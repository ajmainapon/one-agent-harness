"""Core orchestration engine coordinating Claude Code, Antigravity, Codex, and Grok Build CLI."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional

from harness.adapters import BaseAdapter, normalize_path
from harness.adapters.antigravity import AntigravityAdapter
from harness.adapters.claude import ClaudeAdapter
from harness.adapters.codex import CodexAdapter
from harness.adapters.grok import GrokAdapter
from harness.models import ContextBundle, SessionSummary
from harness.platform_utils import atomic_write_text, get_default_agent_homes


MARKER_START = "<!-- ONE-AGENT-HARNESS:START -->"
MARKER_END = "<!-- ONE-AGENT-HARNESS:END -->"

AGENT_ALIASES = {
    "claude": "claude",
    "claude-code": "claude",
    "cc": "claude",
    "antigravity": "antigravity",
    "agy": "antigravity",
    "gemini": "antigravity",
    "codex": "codex",
    "openai": "codex",
    "cdx": "codex",
    "grok": "grok",
    "xai": "grok",
}


def normalize_agent_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    clean = name.strip().lower()
    return AGENT_ALIASES.get(clean, clean)


def is_scratch_cwd(cwd: Optional[str]) -> bool:
    """Return True if cwd is filesystem root, home root, system32, or an auto-created Desktop app scratch directory."""
    if not cwd or cwd.strip() in ("/", "\\"):
        return True
    norm = normalize_path(cwd)
    if not norm or norm in ("/", "\\"):
        return True
    # Windows drive root like "C:\" or "C:/"
    if re.match(r"^[a-zA-Z]:[/\\]?$", norm):
        return True
    home_slash = normalize_path(os.path.expanduser("~")).replace("\\", "/").rstrip("/").lower()
    norm_slash = norm.replace("\\", "/").rstrip("/").lower()
    if norm_slash == home_slash:
        return True
    if norm_slash.endswith("/windows/system32") or "/documents/codex/" in norm_slash or "/claude/scratch-workspaces/" in norm_slash:
        return True
    return False


class HarnessEngine:
    """Unified engine for discovering, extracting, syncing, and converting sessions across agents."""

    def __init__(
        self,
        claude_home: Optional[str] = None,
        agy_home: Optional[str] = None,
        codex_home: Optional[str] = None,
        grok_home: Optional[str] = None,
        harness_home: Optional[str] = None,
    ) -> None:
        self.adapters: Dict[str, BaseAdapter] = {
            "claude": ClaudeAdapter(claude_home=claude_home),
            "antigravity": AntigravityAdapter(agy_home=agy_home),
            "codex": CodexAdapter(codex_home=codex_home),
            "grok": GrokAdapter(grok_home=grok_home),
        }
        self.harness_home = os.path.expanduser(harness_home or get_default_agent_homes()["harness"])
        self.sync_warnings: List[str] = []
        self.checkpoints_file = os.path.join(self.harness_home, "checkpoints.jsonl")

    def list_sessions(
        self,
        agent: Optional[str] = None,
        cwd: Optional[str] = None,
        limit: Optional[int] = 20,
        exclude_agent: Optional[str] = None,
        include_meta: bool = False,
    ) -> List[SessionSummary]:
        """List recent sessions across all or specific agents, sorted newest first."""
        results: List[SessionSummary] = []
        norm_agent = normalize_agent_name(agent)
        norm_exclude = normalize_agent_name(exclude_agent)
        if norm_agent and norm_agent not in self.adapters:
            raise ValueError(f"Unknown agent {agent!r}. Choose claude, antigravity, codex, or grok.")
        target_agents = [norm_agent] if norm_agent else list(self.adapters.keys())

        for ag in target_agents:
            if not ag or (norm_exclude and ag == norm_exclude):
                continue
            adapter = self.adapters.get(ag)
            if not adapter:
                continue
            for s in adapter.list_sessions(cwd=cwd, limit=None):
                if not include_meta and s.is_meta_session:
                    continue
                results.append(s)

        results.sort(key=lambda s: s.mtime, reverse=True)
        return results[:limit]

    def resolve_session(
        self,
        selector: Optional[str] = None,
        source_agent: Optional[str] = None,
        cwd: Optional[str] = None,
        exclude_agent: Optional[str] = None,
    ) -> Optional[SessionSummary]:
        """Resolve a session by 1-based index ('1', '2'), agent alias ('claude'), UUID prefix, or fuzzy title/folder query."""
        norm_agent = normalize_agent_name(source_agent)
        norm_exclude = normalize_agent_name(exclude_agent)
        sel = (selector or "").strip()

        # Check if selector itself is an agent name (e.g., "claude", "codex", "agy")
        if sel and sel.lower() in AGENT_ALIASES and not norm_agent:
            norm_agent = AGENT_ALIASES[sel.lower()]
            sel = ""

        effective_cwd = None if is_scratch_cwd(cwd) else cwd
        all_sessions = self.list_sessions(
            agent=norm_agent,
            cwd=effective_cwd,
            limit=None,
            exclude_agent=norm_exclude if not norm_agent else None,
        )
        if not sel:
            return all_sessions[0] if all_sessions else None

        # Numbers always refer to the same filtered list shown by status/MCP.
        idx_str = sel.lstrip("#")
        if idx_str.isdigit():
            idx = int(idx_str)
            return all_sessions[idx - 1] if 1 <= idx <= len(all_sessions) else None

        sel_lower = sel.lower()
        # Explicit IDs may address synthetic handoffs omitted from the dashboard.
        id_candidates = self.list_sessions(
            agent=norm_agent, cwd=effective_cwd, limit=None,
            exclude_agent=norm_exclude if not norm_agent else None, include_meta=True,
        )
        for session in id_candidates:
            if session.session_id.lower() == sel_lower:
                return session
        id_matches = [s for s in id_candidates if s.session_id.lower().startswith(sel_lower)]
        if len(id_matches) == 1:
            return id_matches[0]
        if len(id_matches) > 1:
            raise ValueError(f"Session ID prefix {sel!r} is ambiguous. Use a longer ID from `harness status`.")

        words = sel_lower.split()
        for session in all_sessions:
            haystack = f"{session.title} {session.cwd} {session.agent}".lower()
            if all(word in haystack for word in words):
                return session

        # Search only the selected scope; an unrelated newer match must not hide it.
        matches = self.search_history(
            query=sel, agent=norm_agent, limit=1, sessions=all_sessions,
        )
        if matches:
            match = matches[0]
            return next((s for s in all_sessions if s.session_id == match["session_id"]
                         and s.agent == match["agent"]), None)
        return None

    def save_checkpoint(
        self,
        summary: str,
        next_steps: str = "",
        cwd: Optional[str] = None,
        agent: str = "agent",
    ) -> Dict[str, Any]:
        """Save an explicit checkpoint note for the next agent."""
        effective_cwd = normalize_path(cwd or os.getcwd())
        entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cwd": effective_cwd,
            "agent": agent,
            "summary": summary.strip(),
            "next_steps": next_steps.strip(),
        }
        os.makedirs(self.harness_home, exist_ok=True)
        with open(self.checkpoints_file, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry) + "\n")
        return entry

    def _load_recent_checkpoints(self, cwd: Optional[str] = None, limit: int = 3) -> List[str]:
        if not os.path.isfile(self.checkpoints_file):
            return []
        notes: List[str] = []
        norm_cwd = normalize_path(cwd) if cwd else ""
        try:
            with open(self.checkpoints_file, "r", encoding="utf-8", errors="replace") as fp:
                lines = [l.strip() for l in fp if l.strip()]
            for line in reversed(lines):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if norm_cwd and obj.get("cwd") and normalize_path(obj["cwd"]) != norm_cwd:
                    continue
                note = f"[{obj.get('timestamp', '')}] {obj.get('summary', '')}"
                if obj.get("next_steps"):
                    note += f" (Next: {obj['next_steps']})"
                notes.append(note)
                if len(notes) >= limit:
                    break
        except OSError:
            pass
        return notes

    def extract_bundle(
        self,
        source_agent: Optional[str] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        exclude_agent: Optional[str] = None,
        max_turns: int = 25,
        query: Optional[str] = None,
    ) -> Optional[ContextBundle]:
        """Extract the full ContextBundle from a specified, queried, or auto-detected session."""
        selector = query or session_id
        resolved = self.resolve_session(
            selector=selector,
            source_agent=source_agent,
            cwd=cwd,
            exclude_agent=exclude_agent,
        )

        # Never silently change the requested source when resolution fails.
        if not resolved:
            return None
        return self.extract_session_bundle(resolved, max_turns=max_turns)

    def extract_session_bundle(
        self,
        resolved: SessionSummary,
        max_turns: int = 25,
        include_other_sessions: bool = True,
    ) -> Optional[ContextBundle]:
        """Extract an already selected session without rediscovering all history."""
        adapter = self.adapters[resolved.agent]
        bundle = adapter.extract_bundle(
            session_id=resolved.session_id,
            max_turns=max_turns,
        )
        if not bundle:
            return None

        bundle.checkpoint_notes = self._load_recent_checkpoints(cwd=bundle.cwd)

        if not include_other_sessions:
            bundle.generate_distilled_summary()
            return bundle

        # Populate other recent substantive sessions across all platforms
        all_recent = self.list_sessions(cwd=None, limit=12, include_meta=False)
        other_list: List[Dict[str, Any]] = []
        seen_agents_enriched: set[str] = set()
        for idx, s in enumerate(all_recent, 1):
            if s.agent == bundle.source_agent and s.session_id == bundle.session_id:
                continue
            s_dict = s.to_dict()
            s_dict["index"] = idx
            if s.agent not in seen_agents_enriched:
                seen_agents_enriched.add(s.agent)
                other_adapter = self.adapters.get(s.agent)
                if other_adapter:
                    try:
                        mini = other_adapter.extract_bundle(session_id=s.session_id, max_turns=2)
                        if mini:
                            s_dict["latest_user"] = (mini.latest_user_request or "").replace("\n", " ")[:220]
                            asst = [
                                (t.final_text or t.content)
                                for t in mini.turns
                                if t.role == "assistant" and (t.final_text or t.content).strip()
                            ]
                            if asst:
                                s_dict["latest_progress"] = asst[-1].replace("\n", " ")[:260]
                    except Exception:
                        pass
            other_list.append(s_dict)
            if len(other_list) >= 6:
                break
        bundle.other_recent_sessions = other_list

        bundle.generate_distilled_summary()
        return bundle

    def sync_workspace_context(
        self,
        bundle: ContextBundle,
        target_cwd: Optional[str] = None,
        update_agent_md: bool = False,
        update_global: bool = True,
    ) -> str:
        """Save context, falling back to harness home if the workspace is unavailable.

        Nonfatal problems are exposed through sync_warnings. If neither location
        can be written, readers can still return the in-memory bundle.
        """
        self.sync_warnings = []
        candidate_cwd = target_cwd if (target_cwd and not is_scratch_cwd(target_cwd)) else bundle.cwd
        effective_cwd = normalize_path(candidate_cwd or "")
        # Older workspace fallbacks must not replace the watcher's newest global context.
        fallback_dir = self.harness_home
        if not update_global:
            workspace_key = hashlib.sha256(effective_cwd.encode("utf-8")).hexdigest()[:16]
            fallback_dir = os.path.join(self.harness_home, "workspaces", workspace_key)
        if not effective_cwd or is_scratch_cwd(effective_cwd):
            effective_cwd = self.harness_home
        elif not os.path.isdir(effective_cwd):
            self.sync_warnings.append(f"Workspace {effective_cwd!r} is unavailable; saving in {fallback_dir!r}.")
            effective_cwd = self.harness_home

        md_content = bundle.to_markdown(include_full_artifacts=True, max_turns=0)
        json_content = json.dumps(bundle.to_dict(), indent=2)

        def save(directory: str) -> str:
            atomic_write_text(os.path.join(directory, "bundle.json"), json_content)
            path = os.path.join(directory, "LIVE_CONTEXT.md")
            atomic_write_text(path, md_content)
            return path

        harness_dir = fallback_dir if effective_cwd == self.harness_home else os.path.join(effective_cwd, ".harness")
        try:
            md_path = save(harness_dir)
        except OSError as exc:
            if effective_cwd == self.harness_home:
                raise
            self.sync_warnings.append(
                f"Could not save context in {harness_dir!r}: {exc}. Saving in {fallback_dir!r} instead."
            )
            effective_cwd = self.harness_home
            md_path = save(fallback_dir)

        global_md = os.path.join(self.harness_home, "LIVE_CONTEXT.md")
        if update_global and os.path.abspath(md_path) != os.path.abspath(global_md):
            try:
                atomic_write_text(global_md, md_content)
            except OSError as exc:
                self.sync_warnings.append(f"Workspace context was saved, but the global copy could not be updated: {exc}.")

        if update_agent_md and effective_cwd != self.harness_home:
            pointer_block = (
                f"{MARKER_START}\n"
                f"## Live Cross-Agent Handoff Context\n"
                f"- Latest active context from **{bundle.source_agent}** (`{bundle.session_id}`, updated `{bundle.updated_at}`) "
                f"is automatically synced in [`.harness/LIVE_CONTEXT.md`](.harness/LIVE_CONTEXT.md).\n"
                f"- When continuing work or running `/handoff`, read `.harness/LIVE_CONTEXT.md` or call the `pull_handoff_context` MCP tool.\n"
                f"{MARKER_END}\n"
            )
            for fname in ("AGENTS.md", "CLAUDE.md"):
                fpath = os.path.join(effective_cwd, fname)
                if os.path.isfile(fpath):
                    try:
                        with open(fpath, "r", encoding="utf-8") as fp:
                            existing = fp.read()
                        if MARKER_START in existing and MARKER_END in existing:
                            start_idx = existing.index(MARKER_START)
                            end_idx = existing.index(MARKER_END) + len(MARKER_END)
                            updated = existing[:start_idx] + pointer_block.strip() + existing[end_idx:]
                        else:
                            updated = existing.rstrip() + "\n\n" + pointer_block
                        atomic_write_text(fpath, updated)
                    except OSError as exc:
                        self.sync_warnings.append(f"Could not update {fpath!r}: {exc}.")

        return md_path

    def switch_session(
        self,
        to_agent: str,
        from_agent: Optional[str] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        max_turns: int = 25,
        dry_run: bool = False,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Extract context from source agent, inject into target agent's native session store, and prepare launch command."""
        target = normalize_agent_name(to_agent) or to_agent.lower()
        if target not in self.adapters:
            raise ValueError(f"Unsupported target agent: {to_agent}. Choose from {list(self.adapters.keys())}")

        bundle = self.extract_bundle(
            source_agent=from_agent,
            session_id=session_id,
            cwd=cwd,
            exclude_agent=target if not (from_agent or session_id or query) else None,
            max_turns=max_turns,
            query=query,
        )
        if not bundle:
            raise RuntimeError("No matching source session found. Run `harness status` with the same filters, then use a session ID or number.")

        effective_cwd = normalize_path(cwd or bundle.cwd or os.getcwd())
        self.sync_warnings = []
        live_path = None
        if not dry_run:
            live_path = self.sync_workspace_context(bundle, target_cwd=effective_cwd, update_agent_md=True)

        injected_id = "dry-run"
        if not dry_run:
            injected_id = self.adapters[target].inject_session(bundle, target_cwd=effective_cwd)

        launch_cmd: List[str] = []
        if target == "claude":
            launch_cmd = ["claude", "--resume", injected_id]
        elif target == "codex":
            launch_cmd = ["codex", "resume", injected_id]
        elif target == "grok":
            launch_cmd = ["grok", f"Continue from handoff context in {live_path or '<context-file>'}"]
        elif target == "antigravity":
            launch_cmd = ["agy", effective_cwd]

        return {
            "source_agent": bundle.source_agent,
            "source_session_id": bundle.session_id,
            "target_agent": target,
            "injected_session_id": injected_id,
            "cwd": effective_cwd,
            "live_context_path": live_path,
            "launch_cmd": launch_cmd,
            "bundle": bundle,
            "warnings": list(self.sync_warnings),
        }

    def search_history(
        self,
        query: str,
        agent: Optional[str] = None,
        limit: int = 15,
        sessions: Optional[List[SessionSummary]] = None,
    ) -> List[Dict[str, Any]]:
        """Search across all 4 agents' conversation turns, titles, and modified files."""
        q_lower = query.lower().strip()
        matches: List[Dict[str, Any]] = []
        if not q_lower:
            return []
        if sessions is None:
            sessions = self.list_sessions(agent=agent, limit=60, include_meta=False)

        for sess in sessions:
            if q_lower in sess.title.lower() or q_lower in sess.cwd.lower():
                matches.append(
                    {
                        "agent": sess.agent,
                        "session_id": sess.session_id,
                        "title": sess.title,
                        "cwd": sess.cwd,
                        "updated_at": sess.updated_at,
                        "match_type": "title/cwd",
                        "snippet": sess.title,
                    }
                )
                if len(matches) >= limit:
                    break
                continue

            adapter = self.adapters.get(sess.agent)
            if not adapter:
                continue
            bundle = adapter.extract_bundle(session_id=sess.session_id, max_turns=30)
            if not bundle:
                continue

            matched_snip = ""
            for fpath in bundle.files_modified + bundle.files_read:
                if q_lower in fpath.lower():
                    matched_snip = f"File touched: {fpath}"
                    break

            if not matched_snip:
                for turn in reversed(bundle.turns):
                    if q_lower in turn.content.lower():
                        idx = turn.content.lower().index(q_lower)
                        start = max(0, idx - 60)
                        end = min(len(turn.content), idx + 140)
                        matched_snip = f"[{turn.role}] ...{turn.content[start:end].replace(chr(10), ' ')}..."
                        break

            if matched_snip:
                matches.append(
                    {
                        "agent": sess.agent,
                        "session_id": sess.session_id,
                        "title": sess.title,
                        "cwd": sess.cwd,
                        "updated_at": sess.updated_at,
                        "match_type": "content",
                        "snippet": matched_snip,
                    }
                )
                if len(matches) >= limit:
                    break

        return matches

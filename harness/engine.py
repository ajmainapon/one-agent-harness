"""Core orchestration engine coordinating Claude Code, Antigravity, Codex, and Grok Build CLI."""

from __future__ import annotations

from datetime import datetime, timezone
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
from harness.platform_utils import get_default_agent_homes


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
    if not cwd:
        return True
    norm = normalize_path(cwd)
    if not norm or norm == "/":
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
        os.makedirs(self.harness_home, exist_ok=True)
        self.checkpoints_file = os.path.join(self.harness_home, "checkpoints.jsonl")

    def list_sessions(
        self,
        agent: Optional[str] = None,
        cwd: Optional[str] = None,
        limit: int = 20,
        exclude_agent: Optional[str] = None,
        include_meta: bool = False,
    ) -> List[SessionSummary]:
        """List recent sessions across all or specific agents, sorted newest first."""
        results: List[SessionSummary] = []
        norm_agent = normalize_agent_name(agent)
        norm_exclude = normalize_agent_name(exclude_agent)
        target_agents = [norm_agent] if norm_agent else list(self.adapters.keys())

        for ag in target_agents:
            if not ag or (norm_exclude and ag == norm_exclude):
                continue
            adapter = self.adapters.get(ag)
            if not adapter:
                continue
            for s in adapter.list_sessions(cwd=cwd, limit=limit * 2):
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

        all_sessions = self.list_sessions(
            agent=norm_agent,
            cwd=None,
            limit=50,
            exclude_agent=norm_exclude if not norm_agent else None,
            include_meta=False,
        )
        if not all_sessions:
            all_sessions = self.list_sessions(
                agent=norm_agent,
                cwd=None,
                limit=20,
                exclude_agent=norm_exclude if not norm_agent else None,
                include_meta=True,
            )

        if not sel:
            effective_cwd_filter = None if is_scratch_cwd(cwd) else cwd
            if effective_cwd_filter:
                cwd_matches = self.list_sessions(
                    agent=norm_agent,
                    cwd=effective_cwd_filter,
                    limit=5,
                    exclude_agent=norm_exclude if not norm_agent else None,
                    include_meta=False,
                )
                if cwd_matches:
                    return cwd_matches[0]
            return all_sessions[0] if all_sessions else None

        # 1. Check 1-based index number ("1", "2", "#3")
        idx_str = sel.lstrip("#")
        if idx_str.isdigit():
            idx = int(idx_str)
            if 1 <= idx <= len(all_sessions):
                return all_sessions[idx - 1]

        # 2. Check exact or prefix session_id match
        sel_lower = sel.lower()
        for s in all_sessions:
            if s.session_id.lower().startswith(sel_lower) or sel_lower in s.session_id.lower():
                return s

        # 3. Fuzzy match against title or workspace path (all words present)
        words = [w for w in sel_lower.split() if w]
        for s in all_sessions:
            haystack = f"{s.title} {s.cwd} {s.agent}".lower()
            if all(w in haystack for w in words):
                return s

        # 4. Any word match or full-text search
        matches = self.search_history(query=sel, agent=norm_agent, limit=1)
        if matches:
            m_sid = matches[0]["session_id"]
            m_ag = matches[0]["agent"]
            for s in all_sessions:
                if s.session_id == m_sid and s.agent == m_ag:
                    return s

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

        if resolved:
            chosen_agent = resolved.agent
            chosen_sid = resolved.session_id
        else:
            chosen_agent = normalize_agent_name(source_agent)
            chosen_sid = session_id

        if not chosen_agent:
            return None

        adapter = self.adapters.get(chosen_agent)
        if not adapter:
            return None

        effective_cwd_filter = None if (chosen_sid or is_scratch_cwd(cwd)) else cwd
        bundle = adapter.extract_bundle(
            session_id=chosen_sid,
            cwd=effective_cwd_filter,
            max_turns=max_turns,
        )
        if not bundle and effective_cwd_filter:
            bundle = adapter.extract_bundle(
                session_id=chosen_sid,
                cwd=None,
                max_turns=max_turns,
            )
        if not bundle:
            return None

        bundle.checkpoint_notes = self._load_recent_checkpoints(cwd=bundle.cwd)

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
    ) -> str:
        """Write .harness/LIVE_CONTEXT.md and .harness/bundle.json into the workspace."""
        candidate_cwd = target_cwd if (target_cwd and not is_scratch_cwd(target_cwd)) else bundle.cwd
        effective_cwd = normalize_path(candidate_cwd or "")
        if (
            not effective_cwd
            or is_scratch_cwd(effective_cwd)
            or not os.path.isdir(effective_cwd)
            or not os.access(effective_cwd, os.W_OK)
        ):
            effective_cwd = self.harness_home

        harness_dir = (
            self.harness_home
            if effective_cwd == self.harness_home
            else os.path.join(effective_cwd, ".harness")
        )
        os.makedirs(harness_dir, exist_ok=True)

        md_path = os.path.join(harness_dir, "LIVE_CONTEXT.md")
        json_path = os.path.join(harness_dir, "bundle.json")

        md_content = bundle.to_markdown(include_full_artifacts=True, max_turns=25)
        with open(md_path, "w", encoding="utf-8") as fp:
            fp.write(md_content)

        with open(json_path, "w", encoding="utf-8") as fp:
            json.dump(bundle.to_dict(), fp, indent=2)

        global_md = os.path.join(self.harness_home, "LIVE_CONTEXT.md")
        if os.path.abspath(md_path) != os.path.abspath(global_md):
            with open(global_md, "w", encoding="utf-8") as fp:
                fp.write(md_content)

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
                        with open(fpath, "w", encoding="utf-8") as fp:
                            fp.write(updated)
                    except OSError:
                        pass

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
            raise RuntimeError("No source session found to hand off.")

        effective_cwd = normalize_path(cwd or bundle.cwd or os.getcwd())
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
            launch_cmd = ["grok", f"Continue from handoff context in {live_path}"]
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
        }

    def search_history(
        self,
        query: str,
        agent: Optional[str] = None,
        limit: int = 15,
    ) -> List[Dict[str, Any]]:
        """Search across all 4 agents' conversation turns, titles, and modified files."""
        q_lower = query.lower().strip()
        matches: List[Dict[str, Any]] = []
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

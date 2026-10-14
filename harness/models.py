"""Canonical context data models and Git workspace inspection utilities."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import subprocess
from typing import Any, Dict, List, Optional


@dataclass
class Turn:
    """A single consolidated conversation turn (user prompt or assistant response)."""

    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: Optional[str] = None
    thinking: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    final_text: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class ArtifactDoc:
    """A plan, task checklist, or artifact associated with a session."""

    name: str
    path: str
    content: str
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class SessionSummary:
    """Lightweight metadata for listing sessions across all platforms."""

    agent: str  # "claude" | "antigravity" | "codex" | "grok"
    session_id: str
    title: str
    cwd: str
    updated_at: str
    mtime: float
    source_path: str
    turn_count: int = 0
    is_meta_session: bool = False  # True if session is only a /handoff test or 1-turn side question

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class ContextBundle:
    """Complete cross-agent context bundle preserving all 4 context dimensions."""

    source_agent: str
    session_id: str
    title: str
    cwd: str
    updated_at: str
    source_path: str = ""
    git_branch: Optional[str] = None
    git_status_summary: str = ""
    git_diff_stat: str = ""
    files_modified: List[str] = field(default_factory=list)
    files_read: List[str] = field(default_factory=list)
    recent_commands_and_errors: List[str] = field(default_factory=list)
    artifacts_and_plans: List[ArtifactDoc] = field(default_factory=list)
    turns: List[Turn] = field(default_factory=list)
    total_turn_count: int = 0
    initial_goal: str = ""
    latest_user_request: str = ""
    distilled_summary: str = ""
    checkpoint_notes: List[str] = field(default_factory=list)
    other_recent_sessions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def generate_distilled_summary(self) -> str:
        """Build a concise executive handoff summary from the extracted turns & state."""
        user_turns = [t for t in self.turns if t.role == "user" and t.content.strip()]
        assistant_turns = [t for t in self.turns if t.role == "assistant" and (t.final_text or t.content).strip()]

        init_goal = (
            self.initial_goal.strip()[:450]
            if self.initial_goal.strip()
            else (user_turns[0].content.strip()[:450] if user_turns else "N/A")
        )
        latest_user = (
            self.latest_user_request.strip()[:550]
            if self.latest_user_request.strip()
            else (user_turns[-1].content.strip()[:550] if user_turns else "N/A")
        )
        if assistant_turns:
            last_asst = assistant_turns[-1]
            best_text = (last_asst.final_text or last_asst.content).strip()
            if len(best_text) > 650:
                best_text = best_text[:250] + " ... " + best_text[-400:]
            latest_assistant = best_text
        else:
            latest_assistant = "N/A"

        lines = [
            f"- **Source Platform**: `{self.source_agent}` (Session `{self.session_id}`)",
            f"- **Session Title**: {self.title}",
            f"- **Working Directory**: `{self.cwd}`",
            f"- **Last Updated**: `{self.updated_at}`",
            f"- **Initial Goal**: {init_goal}",
            f"- **Latest User Request**: {latest_user}",
            f"- **Latest Agent Progress**: {latest_assistant}",
        ]
        if self.files_modified:
            lines.append(
                f"- **Files Modified ({len(self.files_modified)})**: "
                + ", ".join(f"`{p}`" for p in self.files_modified[:15])
            )
        if self.artifacts_and_plans:
            lines.append(
                f"- **Active Plans/Artifacts ({len(self.artifacts_and_plans)})**: "
                + ", ".join(f"`{a.name}`" for a in self.artifacts_and_plans)
            )
        if self.checkpoint_notes:
            lines.append("- **Explicit Checkpoint Notes**: " + " | ".join(self.checkpoint_notes))

        self.distilled_summary = "\n".join(lines)
        return self.distilled_summary

    def to_markdown(self, include_full_artifacts: bool = True, max_turns: int = 25) -> str:
        """Render the complete ContextBundle into a structured Markdown handoff document."""
        if not self.distilled_summary:
            self.generate_distilled_summary()

        sections: List[str] = []
        sections.append(
            f"# Cross-Agent Context Handoff\n"
            f"> Automatically synchronized by **One Agent Harness** at `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}`\n"
        )

        # 1. Distilled Handoff Summary
        sections.append("## 1. Distilled Handoff Summary\n" + self.distilled_summary + "\n")

        # 2. Codebase, Tool Calls & Git State
        code_lines = ["## 2. Codebase, Tool Calls & Git State"]
        if self.git_branch:
            code_lines.append(f"- **Git Branch**: `{self.git_branch}`")
        if self.files_modified:
            code_lines.append("\n### Files Modified in Session")
            for fpath in self.files_modified[:30]:
                code_lines.append(f"- `{fpath}`")
        if self.files_read:
            code_lines.append("\n### Key Files Read / Inspected")
            for fpath in self.files_read[:25]:
                code_lines.append(f"- `{fpath}`")
        if self.recent_commands_and_errors:
            code_lines.append("\n### Recent Commands, Tool Actions & Errors")
            for cmd in self.recent_commands_and_errors[-15:]:
                code_lines.append(f"- {cmd}")
        if self.git_status_summary:
            code_lines.append("\n### Uncommitted Git Status (`git status -s`)")
            code_lines.append(f"```text\n{self.git_status_summary.strip()}\n```")
        if self.git_diff_stat:
            code_lines.append("\n### Git Diff Stat (`git diff --stat`)")
            code_lines.append(f"```text\n{self.git_diff_stat.strip()}\n```")
        sections.append("\n".join(code_lines) + "\n")

        # 3. Active Implementation Plans, Task Checklists & Artifacts
        if self.artifacts_and_plans:
            plan_lines = ["## 3. Active Plans, Task Checklists & Artifacts"]
            for art in self.artifacts_and_plans:
                plan_lines.append(f"\n### `{art.name}` (`{art.path}`)")
                if include_full_artifacts:
                    content_preview = art.content.strip()
                    if len(content_preview) > 12000:
                        content_preview = content_preview[:12000] + "\n... [truncated]"
                    plan_lines.append(f"```markdown\n{content_preview}\n```")
            sections.append("\n".join(plan_lines) + "\n")

        # 4. Verbatim Recent Conversation Turns
        selected_turns = self.turns[-max_turns:] if max_turns and len(self.turns) > max_turns else self.turns
        total_count = self.total_turn_count or len(self.turns)
        turn_lines = [
            f"## 4. Verbatim Conversation Transcript (Last {len(selected_turns)} of {total_count} Turns)"
        ]
        for idx, turn in enumerate(selected_turns, 1):
            role_label = turn.role.upper()
            ts_str = f" ({turn.timestamp})" if turn.timestamp else ""
            turn_lines.append(f"\n### Turn {idx} — {role_label}{ts_str}")
            if turn.thinking:
                thinking_short = turn.thinking.strip()
                if len(thinking_short) > 600:
                    thinking_short = thinking_short[:600] + "..."
                turn_lines.append(f"> *Reasoning*: {thinking_short}\n")
            if turn.content.strip():
                content_text = turn.content.strip()
                if len(content_text) > 4000:
                    content_text = (
                        content_text[:1500]
                        + "\n\n... [intermediate steps omitted] ...\n\n"
                        + content_text[-2500:]
                    )
                turn_lines.append(content_text)
            if turn.tool_calls:
                tc_summaries = []
                for tc in turn.tool_calls[:10]:
                    tname = tc.get("name", "tool")
                    t_arg = tc.get("summary", "")
                    tc_summaries.append(f"`{tname}`({t_arg})" if t_arg else f"`{tname}`")
                turn_lines.append("\n*Tools invoked*: " + ", ".join(tc_summaries))
        sections.append("\n".join(turn_lines) + "\n")

        # 5. Other Recent Sessions Across All Agents (if available)
        if self.other_recent_sessions:
            other_lines = [
                "## 5. Other Recent Sessions Across All Platforms",
                "> If the user meant one of these other recent sessions, call `pull_handoff_context(source_agent=..., session_id=...)`:",
            ]
            for s in self.other_recent_sessions[:6]:
                other_lines.append(
                    f"- **[{s['agent'].upper()}]** `{s['session_id']}` — **{s['title']}** "
                    f"(Updated: `{s['updated_at']}`, CWD: `{s['cwd'] or 'N/A'}`)"
                )
                if s.get("latest_user"):
                    other_lines.append(f"  - *Latest Prompt*: {s['latest_user']}")
                if s.get("latest_progress"):
                    other_lines.append(f"  - *Latest Progress*: {s['latest_progress']}")
            sections.append("\n".join(other_lines) + "\n")

        return "\n".join(sections)


def inspect_git_state(cwd: str) -> Dict[str, Optional[str]]:
    """Inspect git branch, status, and diff stat for a workspace directory."""
    result: Dict[str, Optional[str]] = {
        "branch": None,
        "status": "",
        "diff_stat": "",
    }
    if not cwd or not os.path.isdir(cwd):
        return result

    def _run_git(args: List[str]) -> str:
        try:
            proc = subprocess.run(
                ["git"] + args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=2.5,
                check=False,
            )
            if proc.returncode == 0:
                return proc.stdout.strip()
        except Exception:
            pass
        return ""

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch:
        result["branch"] = branch
        result["status"] = _run_git(["status", "-s"])[:2000]
        result["diff_stat"] = _run_git(["diff", "--stat"])[:2000]
    return result

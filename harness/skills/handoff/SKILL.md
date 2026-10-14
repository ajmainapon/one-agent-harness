---
name: handoff
description: Pull exact context (verbatim conversation turns, active implementation plans, task lists, modified files, terminal errors, and git state) from Claude Code, Antigravity, Codex, or Grok Build CLI, or prepare a handoff to another agent. Activate when the user types /handoff, $handoff, or asks to continue a session from another AI coding tool.
---

# Cross-Agent Context Handoff (`/handoff` & `$handoff`)

Use this skill whenever the user wants to:
1. **Continue work from another agent** (e.g., *"continue my last Claude Code session"*, *"pull context from Antigravity"*, *"what was I doing in Codex?"*, `/handoff`, `/handoff auth refactor`, `/handoff 2`, or `$handoff`).
2. **Switch or hand off current work to another agent** (e.g., *"hand this off to Codex"*, *"switch to Claude Code"*).

## Step 1: Retrieve the Exact Context Bundle

1. **If the `one-agent-harness` MCP server is available**:
   - Call the MCP tool `pull_handoff_context`:
     - If the user passed a session number, project name, or keyword (e.g. `/handoff 2`, `/handoff auth refactor`, `$handoff api-server`), pass it in `query` (e.g. `query="auth refactor"` or `query="2"`).
     - If the user named a platform (`"claude"`, `"antigravity"`, `"codex"`, or `"grok"`), pass `source_agent` (or `query`).
     - Pass `cwd` if you are in a real project repository.
   - Note: `pull_handoff_context` automatically skips trivial `/handoff` test sessions, ignores blank scratch folders (like `~/Documents/Codex/...`), and includes **Section 5: Other Recent Sessions Across All Platforms** with the latest prompt and progress from each agent.
2. **Fallback via CLI (works immediately on Linux, macOS, and Windows without restarting the app)**:
   - Run:
     ```bash
     harness handoff --stdout --no-copy || ~/.local/bin/harness handoff --stdout --no-copy
     ```
     *(Or pass a query/number/platform directly: `harness handoff "auth refactor" --stdout --no-copy` or `harness handoff 2 --stdout --no-copy`; on Windows PowerShell/CMD: `harness handoff --stdout --no-copy` or `"$HOME\.local\bin\harness.cmd" handoff --stdout --no-copy`).*

## Step 2: Assimilate & Resume Seamlessly

Once you have retrieved the handoff context bundle:
1. **Do NOT ask the user to re-explain the problem.**
2. Briefly confirm what you loaded (3–5 bullet points):
   - Source platform, session title, and working directory
   - Current goal & active implementation plan / task step
   - Files already modified and uncommitted git changes
   - Exact next step where the previous agent stopped
   - *(If the user opened a blank scratch window and has multiple active projects in Section 5, briefly mention the other active sessions from Section 5 in case they want to switch to one of those instead — e.g. "Say `/handoff 2` or `/handoff <project>` to load another session").*
3. **Immediately continue executing the next step** of the task (or answer the user's prompt using the restored context).

# Contributing to One Agent Harness

Thank you for contributing to **One Agent Harness (`harness`)**! Our goal is to make context switching between agentic AI coding tools effortless across **Linux, macOS, and Windows**.

---

## Architecture Overview

- `harness/models.py`: Canonical data structures (`Turn`, `ArtifactDoc`, `SessionSummary`, `ContextBundle`) and 5-section Markdown serialization.
- `harness/platform_utils.py`: Cross-platform abstractions for **Linux**, **macOS**, and **Windows** (clipboard via `pbcopy`/`Set-Clipboard`/`wl-copy`/`xclip`/`xsel`/OSC 52, UTF-8 & Windows Virtual Terminal console setup, detached daemon spawning, PID probing, and read-only SQLite URI handling).
- `harness/adapters/`: Platform-specific session readers and native session writers:
  - `claude.py` — Claude Code (`~/.claude/projects` & `~/.claude/plans`)
  - `antigravity.py` — Antigravity (`~/.gemini/antigravity/brain` & `conversation_summaries.db`)
  - `codex.py` — OpenAI Codex (`~/.codex/sessions`, `session_index.jsonl`, & `state_5.sqlite`)
  - `grok.py` — Grok Build CLI (`~/.grok/sessions`)
- `harness/engine.py`: Unified orchestration engine (`list_sessions`, `resolve_session`, `extract_bundle`, `sync_workspace_context`, `switch_session`, `search_history`).
- `harness/mcp_server.py`: Zero-dependency STDIO JSON-RPC 2.0 Model Context Protocol (MCP) server supporting both newline-delimited and `Content-Length` framed transports.
- `harness/watcher.py`: Background Auto-Sync Watcher daemon.
- `harness/cli.py`: Command-line interface (`harness`).

---

## Adding a New Agent Adapter

1. Create `harness/adapters/<agent>.py` subclassing `BaseAdapter` (`list_sessions`, `extract_bundle`, `inject_session`).
2. Register the default home directory in `harness/platform_utils.py` (`get_default_agent_homes()`).
3. Register the adapter and aliases in `harness/engine.py` (`HarnessEngine.__init__` and `AGENT_ALIASES`).
4. Add unit and integration test fixtures in `tests/test_harness.py`.

---

## Running Tests

One Agent Harness uses zero external runtime dependencies (100% Python 3.9+ standard library):

```bash
python3 -m unittest discover -s tests -v
```

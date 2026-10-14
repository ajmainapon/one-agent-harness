# One Agent Harness (`harness`)

**Universal Cross-Agent Context Bridge for Claude Code, Antigravity, OpenAI Codex, and Grok Build CLI.**

[![CI (Linux · macOS · Windows)](https://github.com/ajmainapon/one-agent-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/ajmainapon/one-agent-harness/actions/workflows/ci.yml)
[![Platform: Linux | macOS | Windows](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-blue)](#installation-linux--macos--windows)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776ab)](#installation-linux--macos--windows)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-success)](#architecture)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Never re-explain your problem or manually copy-paste context when switching AI coding tools. **One Agent Harness** extracts your exact verbatim conversation turns, active implementation plans, task lists, modified files, terminal errors, and uncommitted git state from any agent and injects or hands them off to any other agent in one command.

---

## Supported Platforms & Operating Systems

| AI Coding Agent | Session & Artifact Storage | Native Session Injection | MCP & `/handoff` / `$handoff` |
| :--- | :--- | :---: | :---: |
| **Claude Code** | `~/.claude/projects/` & `~/.claude/plans/` | ✅ `claude --resume <id>` | ✅ `/handoff` |
| **Antigravity** | `~/.gemini/antigravity/brain/` & `conversation_summaries.db` | ✅ `agy <workspace>` | ✅ `/handoff` |
| **OpenAI Codex** | `~/.codex/sessions/`, `session_index.jsonl`, & `state_5.sqlite` | ✅ `codex resume <id>` | ✅ `$handoff` |
| **Grok Build CLI** | `~/.grok/sessions/` | ✅ `grok ...` | ✅ `/handoff` |

Works out of the box on **Linux** (X11, Wayland, WSL2, SSH/OSC 52), **macOS**, and **Windows** (PowerShell, CMD, Windows Terminal, Git Bash) with **zero external runtime dependencies** (100% Python 3.9+ standard library).

---

## Installation (Linux · macOS · Windows)

### Option 1: One-Line Bootstrap Installer (Recommended)

**Linux & macOS (bash / zsh / sh):**
```bash
git clone https://github.com/ajmainapon/one-agent-harness.git
cd one-agent-harness
chmod +x install.sh && ./install.sh
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/ajmainapon/one-agent-harness.git
cd one-agent-harness
.\install.ps1
```

### Option 2: Standard Python Package (`pipx`, `uv`, or `pip`)

```bash
# Install globally with pipx or uv (Linux, macOS, Windows)
pipx install .
# or: uv tool install .
# or: pip install -e .

# Register MCP server + /handoff & $handoff skills across all 4 agents
harness install

# Start background Auto-Sync Watcher daemon
harness watch --daemon
```

---

## Usage Guide

### 1. Inside Any Agent Chat (`/handoff` & `$handoff`)

No need to remember session UUIDs! Pass a **session number (`1`, `2`, `3`)**, **project name**, **keyword**, or **agent name**:

- **In Claude Code, Antigravity, or Grok Build CLI**:
  - `/handoff` *(loads the latest active session)*
  - `/handoff my-project` *(fuzzy-matches your project or session title)*
  - `/handoff 2` *(loads session `#2` from the recent sessions list)*
  - `/handoff claude` *(loads your latest Claude Code session)*
- **In OpenAI Codex (`v0.162.1+`)**:
  - `$handoff`, `$handoff my-project`, or `$handoff 2` *(Codex reserves `/` for built-in TUI commands and uses `$` for skills)*.

---

### 2. Zero-Flag Terminal Shorthands

Run `harness` (with no arguments) to see the colorized dashboard with numbered sessions (`#1`, `#2`, `#3`, ...):

```bash
# View colorized dashboard of all recent sessions across all 4 platforms
harness

# Interactive menu — pick a session & action by number
harness pick

# 1-command switch into an agent (accepts session number, project keyword, or agent alias)
harness codex                 # Inject latest session into Codex & launch
harness codex 3               # Inject session #3 into Codex & launch
harness codex "auth refactor" # Inject session matching "auth refactor" into Codex & launch
harness claude 2              # Inject session #2 into Claude Code & launch
harness agy 1                 # Inject session #1 into Antigravity

# Copy any session's full handoff bundle to system clipboard
harness copy                  # Copy #1 latest session
harness copy 2                # Copy session #2
harness copy "auth"           # Copy session matching "auth"

# Search across all 4 platforms' transcripts & touched files
harness search "keyword"
```

---

### 3. Environment Variable Overrides

All storage paths default to standard user home directories on Linux, macOS, and Windows (`~` / `%USERPROFILE%`), and can be customized via environment variables:

| Environment Variable | Default Path | Description |
| :--- | :--- | :--- |
| `HARNESS_CLAUDE_HOME` / `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code data directory |
| `HARNESS_AGY_HOME` / `ANTIGRAVITY_HOME` | `~/.gemini/antigravity` | Antigravity data directory |
| `HARNESS_CODEX_HOME` / `CODEX_HOME` | `~/.codex` | OpenAI Codex data directory |
| `HARNESS_GROK_HOME` / `GROK_HOME` | `~/.grok` | Grok Build CLI data directory |
| `HARNESS_HOME` | `~/.harness` | One Agent Harness state, logs, and checkpoints |

---

## Running the Test Suite

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT License — see [LICENSE](LICENSE).

"""Cross-platform installer for registering One Agent Harness across Claude Code, Antigravity, Codex, and Grok on Linux, macOS, and Windows."""

from __future__ import annotations

from datetime import datetime
import json
import os
import shutil
import stat
import sys
from typing import Dict, List, Optional

from harness.platform_utils import IS_WINDOWS, atomic_write_text


def _backup_file(fpath: str) -> Optional[str]:
    if not os.path.isfile(fpath) or os.path.getsize(fpath) == 0:
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    bak_path = f"{fpath}.bak.{ts}"
    shutil.copy2(fpath, bak_path)
    return bak_path


def _read_mcp_config(path: str) -> dict:
    """Validate existing settings before making any installation changes."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            config = json.load(fp)
        if not isinstance(config, dict):
            raise ValueError("the top-level value must be an object")
        if "mcpServers" in config and not isinstance(config["mcpServers"], dict):
            raise ValueError("mcpServers must be an object")
        return config
    except (ValueError, UnicodeError) as exc:
        raise ValueError(
            f"Cannot update {path!r}: {exc}. Existing settings were left unchanged. "
            "Repair this JSON file and run `harness install` again."
        ) from exc


def install_all(
    project_root: Optional[str] = None,
    home_dir: Optional[str] = None,
    is_windows: Optional[bool] = None,
) -> Dict[str, List[str]]:
    """Install CLI executables, MCP server configs, and /handoff & $handoff skills across all 4 agents on Linux, macOS, and Windows."""
    root = project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home = os.path.expanduser(home_dir) if home_dir else os.path.expanduser("~")
    win_mode = IS_WINDOWS if is_windows is None else bool(is_windows)
    python_bin = sys.executable

    installed_items: Dict[str, List[str]] = {
        "cli_binaries": [],
        "mcp_configs": [],
        "skills": [],
        "backups": [],
    }

    # Preflight every JSON config before copying runtime files or changing settings.
    config_paths = [
        os.path.join(home, ".claude.json"),
        os.path.join(home, ".gemini", "config", "mcp_config.json"),
        os.path.join(home, ".grok", "mcp.json"),
    ]
    configs = {path: _read_mcp_config(path) for path in config_paths}

    # 1. Install a permanent self-contained copy of the `harness` package into ~/.harness/lib/harness
    # so `harness` keeps working even if the user later deletes or moves the git clone folder!
    installed_lib = os.path.join(home, ".harness", "lib")
    os.makedirs(installed_lib, exist_ok=True)
    pkg_src = os.path.join(root, "harness")
    pkg_dst = os.path.join(installed_lib, "harness")
    if os.path.isdir(pkg_src) and os.path.abspath(pkg_src) != os.path.abspath(pkg_dst):
        if os.path.exists(pkg_dst):
            shutil.rmtree(pkg_dst, ignore_errors=True)
        shutil.copytree(
            pkg_src,
            pkg_dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

    # 2. Install CLI shims into ~/.local/bin (works on Linux, macOS, Windows CMD/PowerShell, and Git Bash/WSL)
    local_bin = os.path.join(home, ".local", "bin")
    os.makedirs(local_bin, exist_ok=True)

    harness_bin = os.path.join(local_bin, "harness")
    mcp_bin = os.path.join(local_bin, "one-agent-harness-mcp")

    root_posix = root.replace("\\", "/")
    lib_posix = installed_lib.replace("\\", "/")
    py_posix = python_bin.replace("\\", "/")

    harness_script = f"""#!/bin/sh
export PYTHONPATH="{root_posix}:{lib_posix}:${{PYTHONPATH}}"
exec "{py_posix}" -m harness.cli "$@"
"""
    mcp_script = f"""#!/bin/sh
export PYTHONPATH="{root_posix}:{lib_posix}:${{PYTHONPATH}}"
exec "{py_posix}" -m harness.mcp_server "$@"
"""

    for path, content in [(harness_bin, harness_script), (mcp_bin, mcp_script)]:
        with open(path, "w", encoding="utf-8", newline="\n") as fp:
            fp.write(content)
        try:
            st = os.stat(path)
            os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass
        installed_items["cli_binaries"].append(path)

    if win_mode:
        # Also create Windows .cmd and .ps1 launchers so CMD, PowerShell, and Windows GUI MCP clients work natively
        harness_cmd = os.path.join(local_bin, "harness.cmd")
        mcp_cmd = os.path.join(local_bin, "one-agent-harness-mcp.cmd")
        harness_ps1 = os.path.join(local_bin, "harness.ps1")

        cmd_harness_body = (
            "@echo off\r\n"
            f'set "PYTHONPATH={root};{installed_lib};%PYTHONPATH%"\r\n'
            f'"{python_bin}" -m harness.cli %*\r\n'
        )
        cmd_mcp_body = (
            "@echo off\r\n"
            f'set "PYTHONPATH={root};{installed_lib};%PYTHONPATH%"\r\n'
            f'"{python_bin}" -m harness.mcp_server %*\r\n'
        )
        ps1_harness_body = (
            f'$env:PYTHONPATH = "{root};{installed_lib};" + $env:PYTHONPATH\r\n'
            f'& "{python_bin}" -m harness.cli @args\r\n'
        )

        for path, body in [
            (harness_cmd, cmd_harness_body),
            (mcp_cmd, cmd_mcp_body),
            (harness_ps1, ps1_harness_body),
        ]:
            with open(path, "w", encoding="utf-8") as fp:
                fp.write(body)
            installed_items["cli_binaries"].append(path)

        mcp_exec_path = mcp_cmd
    else:
        mcp_exec_path = mcp_bin

    mcp_entry = {
        "command": mcp_exec_path,
        "args": [],
    }

    # Preserve and back up every JSON config, including Grok, before updating it.
    for config_path, config in configs.items():
        backup = _backup_file(config_path)
        if backup:
            installed_items["backups"].append(backup)
        config.setdefault("mcpServers", {})["one-agent-harness"] = mcp_entry
        atomic_write_text(config_path, json.dumps(config, indent=2) + "\n")
        installed_items["mcp_configs"].append(config_path)

    # 4. Register MCP Server in Codex (~/.codex/config.toml)
    codex_dir = os.path.join(home, ".codex")
    os.makedirs(codex_dir, exist_ok=True)
    codex_toml_path = os.path.join(codex_dir, "config.toml")
    # Use forward slashes in TOML string so Windows paths like C:\Users\... never trigger \U escape errors
    mcp_toml_cmd = mcp_exec_path.replace("\\", "/")
    if os.path.isfile(codex_toml_path):
        bak = _backup_file(codex_toml_path)
        if bak:
            installed_items["backups"].append(bak)
        with open(codex_toml_path, "r", encoding="utf-8") as fp:
            toml_text = fp.read()
    else:
        toml_text = ""

    if "[mcp_servers.one-agent-harness]" not in toml_text:
        toml_block = (
            f"\n[mcp_servers.one-agent-harness]\n"
            f'command = "{mcp_toml_cmd}"\n'
            f"args = []\n"
        )
        with open(codex_toml_path, "a", encoding="utf-8") as fp:
            fp.write(toml_block)
    installed_items["mcp_configs"].append(codex_toml_path)

    # 6. Install /handoff skill across all agents
    skill_src = os.path.join(root, "harness", "skills", "handoff", "SKILL.md")
    skill_targets = [
        os.path.join(home, ".claude", "skills", "handoff", "SKILL.md"),
        os.path.join(home, ".gemini", "antigravity", "skills", "handoff", "SKILL.md"),
        os.path.join(home, ".agents", "skills", "handoff", "SKILL.md"),
        os.path.join(home, ".codex", "skills", "handoff", "SKILL.md"),
        os.path.join(home, ".grok", "skills", "handoff", "SKILL.md"),
    ]
    if os.path.isfile(skill_src):
        for dst in skill_targets:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(skill_src, dst)
            installed_items["skills"].append(dst)

    # 7. Install Codex-specific agents/openai.yaml so `$handoff` and `/skills` menu work in Codex
    openai_yaml_content = (
        "interface:\n"
        '  display_name: "Cross-Agent Handoff"\n'
        '  short_description: "Pull exact context from Claude Code, Antigravity, Codex, or Grok"\n'
        '  default_prompt: "Use $handoff to pull my latest session context and continue where I left off."\n'
    )
    for codex_skill_dir in [
        os.path.join(home, ".codex", "skills", "handoff"),
        os.path.join(home, ".agents", "skills", "handoff"),
    ]:
        agents_dir = os.path.join(codex_skill_dir, "agents")
        os.makedirs(agents_dir, exist_ok=True)
        yaml_path = os.path.join(agents_dir, "openai.yaml")
        with open(yaml_path, "w", encoding="utf-8") as fp:
            fp.write(openai_yaml_content)
        installed_items["skills"].append(yaml_path)

    # 8. Install Claude Code custom slash command (~/.claude/commands/handoff.md) so `/handoff` works natively in Claude
    claude_cmd_dir = os.path.join(home, ".claude", "commands")
    os.makedirs(claude_cmd_dir, exist_ok=True)
    claude_cmd_path = os.path.join(claude_cmd_dir, "handoff.md")
    if os.path.isfile(skill_src):
        shutil.copy2(skill_src, claude_cmd_path)
        installed_items["skills"].append(claude_cmd_path)

    return installed_items
